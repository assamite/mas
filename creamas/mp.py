'''
.. py:module:: mp
    :platform: Unix

This module contains multiprocessing implementation for
:class:`~creamas.core.environment.Environment`,
:class:`~creamas.mp.MultiEnvironment`.

A :class:`~creamas.mp.MultiEnvironment` holds several
:class:`~creamas.core.environment.Environment` slaves, which are spawned on
their own processes, and uses managers to obtain much of the same functionality
as the single processor environment. See :class:`~creamas.mp.EnvManager` and
:class:`~creamas.mp.MultiEnvManager` for details.

.. warning::
    This functionality is currently largely untested. However, it *seems* to
    work as intended and may be used in
    :class:`~creamas.core.simulation.Simulation`.
'''
import asyncio
import logging
import multiprocessing
import operator
import time
import itertools

from collections import Counter
from random import shuffle

import aiomas
from aiomas.agent import _get_base_url

from creamas.logging import ObjectLogger
from creamas.core.environment import Environment
from creamas import util


logger = logging.getLogger(__name__)
TIMEOUT = 5


class EnvManager(aiomas.subproc.Manager):
    """A manager for :class:`~creamas.core.environment.Environment`, subclass
    of :class:`aiomas.subproc.Manager`.

    Managers are used in environments which need to be able to execute
    commands originating from outside sources, e.g. in slave environments
    inside a multiprocessing environment.

    A manager can spawn other agents into its environment, and can execute
    other tasks relevant to the environment. The manager should always be the
    first agent created to the environment.

    .. note::
        You should not need to create managers directly, instead pass the
        desired manager class to an instance of
        :class:`~creamas.mp.MultiEnvironment` at its initialization time.
    """
    def __init__(self, environment):
        super().__init__(environment)
        self._host_manager = None

    @property
    def env(self):
        return self.container

    @aiomas.expose
    def set_host_manager(self, addr):
        '''Set host (or master) manager for this manager.

        :param addr:
            Address for the host manager.
        '''
        self._host_manager = addr

    @aiomas.expose
    def host_manager(self):
        '''Get address of the host manager.
        '''
        return self._host_manager

    @aiomas.expose
    async def report(self, msg, timeout=5):
        '''Report message to the host manager.
        '''
        try:
            host_manager = await self.env.connect(self.host_manager,
                                                  timeout=timeout)
        except:
            raise ConnectionError("Could not reach host manager ({})."
                                  .format(self.host_manager))
        ret = await host_manager.handle(msg)
        return ret

    @aiomas.expose
    def handle(self, msg):
        '''Handle message, override in subclass if needed.'''
        pass

    @aiomas.expose
    def get_agents(self, addr=True, agent_cls=None):
        '''Get agents from the managed environment.

        This is a managing function for the
        :py:meth:`~creamas.environment.Environment.get_agents`. Returned
        agent list excludes the environment's manager agent (this agent) by
        design.
        '''
        return self.env.get_agents(addr=addr, agent_cls=agent_cls)

    @aiomas.expose
    def set_log_folder(self, log_folder):
        self.env.log_folder = log_folder

    @aiomas.expose
    def stop(self, folder=None):
        '''Stop the managed environment, close all the agents and set
        stop_received on this agent to True.
        '''
        ret = self.env.save_info(folder)
        for a in self.get_agents(addr=False):
            a.close(folder=folder)
        self.stop_received.set_result(True)
        return ret

    @aiomas.expose
    def candidates(self):
        '''Return candidates from the managed environment.
        '''
        return self.env.candidates

    @aiomas.expose
    def artifacts(self):
        '''Return artifacts from the managed environment.
        '''
        return self.env.artifacts

    @aiomas.expose
    def create_connections(self, connection_map):
        '''Create connections for agents in the environment.

        This is a managing function for
        :meth:`~creamas.core.environment.Environment.create_connections`.
        '''
        return self.env.create_connections(connection_map)

    @aiomas.expose
    def get_connections(self, attitudes=True):
        '''Get connections from the agents in the environment.

        This is a managing function for
        :meth:`~creamas.core.environment.Environment.get_connections`.
        '''
        return self.env.get_connections(attitudes=attitudes)

    @aiomas.expose
    def validate(self, candidates):
        '''Returns the candidate list unaltered.

        Implemented for consistency.
        '''
        return candidates

    @aiomas.expose
    def validate_candidates(self, candidates):
        '''Validate the candidates with the agents in the managed environment.
        '''
        self.env._candidates = candidates
        self.env.validate_candidates()
        return self.env.candidates

    @aiomas.expose
    def clear_candidates(self):
        '''Clear candidates in the managed environment.

        This is a managing function for
        :py:meth:`~creamas.environment.Environment.clear_candidates`.
        '''
        self.env.clear_candidates()

    @aiomas.expose
    def vote(self, candidates):
        '''Vote for candidates. Manager votes each candidate similarly.

        Implemented for consistency.
        '''
        cands = candidates
        votes = [(c, 1.0) for c in cands]
        return votes

    @aiomas.expose
    def get_votes(self, candidates):
        self.env._candidates = candidates
        votes = self.env._gather_votes()
        return votes

    @aiomas.expose
    async def add_candidate(self, artifact):
        '''Add candidate to the host manager's list of candidates.
        '''
        host_manager = await self.env.connect(self._host_manager)
        host_manager.add_candidate(artifact)

    @aiomas.expose
    async def get_artifacts(self):
        '''Get all artifacts from the host environment.

        :returns: All the artifacts in the environment.
        '''
        host_manager = await self.env.connect(self._host_manager,
                                              timeout=TIMEOUT)
        artifacts = await host_manager.get_artifacts()
        return artifacts

    @aiomas.expose
    def close(self, folder=None):
        '''Implemented for consistency. This basic implementation does nothing.
        '''
        pass

    @aiomas.expose
    async def trigger_all(self, *args, **kwargs):
        '''Trigger all agents in the managed environment to act once.

        This is a managing function for
        :meth:`~creamas.core.environment.Environment.trigger_all`.
        '''
        rets = await self.env.trigger_all(*args, **kwargs)
        return rets

    @aiomas.expose
    async def is_ready(self):
        '''Check if the managed environment is ready.

        This is a managing function for
        :py:meth:`~creamas.environment.Environment.is_ready`.
        '''
        return self.env.is_ready()

    @aiomas.expose
    async def spawn_n(self, agent_cls, n, *args, **kwargs):
        '''Spawn *n* agents to the managed environment. This is a convenience
        function so that one does not have to repeatedly make connections to
        the environment to spawn multiple agents with the same parameters.

        See :py:meth:`~creamas.mp.EnvManager.spawn` for details.
        '''
        rets = []
        for _ in range(n):
            ret = await self.spawn(agent_cls, *args, **kwargs)
            rets.append(ret)
        return rets


class MultiEnvManager(aiomas.subproc.Manager):
    """A manager for :class:`~creamas.mp.MultiEnvironment`, subclass of
    :class:`aiomas.subproc.Manager`.

    A Manager can spawn other agents into its slave environments, and can
    execute other tasks relevant to the whole environment. The manager should
    always be the first (and usually only) agent created for the
    multi-environment's managing environment. The actual simulation agents
    should be created to the slave environments, typically using
    multi-environment's or its manager's functionality.

    .. note::
        You should not need to create managers directly, instead pass the
        desired manager class to an instance of
        :class:`~creamas.mp.MultiEnvironment` at its initialization time.
    """
    def __init__(self, environment):
        super().__init__(environment)

    @property
    def env(self):
        return self.container

    @aiomas.expose
    def handle(self, msg):
        '''Handle message. Override in subclass if needed.
        '''
        pass

    @aiomas.expose
    async def spawn(self, addr, agent_cls, *agent_args, **agent_kwargs):
        '''Spawn an agent to an environment in a manager in the given address.

        *agent_args* and *agent_kwargs* are passed to the manager doing to
        spawning to be used as the agent's initialization parameters.

        :param str addr: Environment's manager's address

        :param agent_cls:
            Class of the agent as a string, e.g. creamas.grid:GridAgent

        :returns: :class:`Proxy` and port of the spawned agent
        '''
        remote_manager = await self.env.connect(addr, timeout=TIMEOUT)
        proxy, port = await remote_manager.spawn(agent_cls, *agent_args,
                                                 **agent_kwargs)
        return proxy, port

    @aiomas.expose
    async def spawn_n(self, addr, agent_cls, n, *agent_args, **agent_kwargs):
        '''Same as :meth:`~creamas.mp.MultiEnvManager.spawn`, but spawn
        multiple agents with same initialization parameters.

        This should considerably reduce the time needed to spawn a large number
        of homogeneous agents.

        *agent_args* and *agent_kwargs* are passed to the manager doing the
        spawning to be used as agents initialization parameters.

        :param str addr: Environment's manager's address

        :param agent_cls:
            Class of the agent as a string, e.g. creamas.grid:GridAgent

        :param int n: Number of agents to spawn.

        :returns: List of (:class:`Proxy`, port) tuples for the spawned agents.

        ... seealso::

            :meth:`creamas.mp.EnvManager.spawn_n`
        '''
        remote_manager = await self.env.connect(addr, timeout=TIMEOUT)
        rets = await remote_manager.spawn_n(agent_cls, n, *agent_args,
                                            **agent_kwargs)
        return rets

    @aiomas.expose
    async def get_agents(self, addr=True, agent_cls=None):
        '''Get all agents in all the slave environments.

        This is a managing function for
        :meth:`creamas.mp.MultiEnvironment.get_agents`.
        '''
        return await self.menv.get_agents(addr=addr, agent_cls=agent_cls)

    @aiomas.expose
    async def get_slave_agents(self, manager_addr, addr=True, agent_cls=None):
        '''Get agents in the specified manager's environment.

        :param str manager_addr: Address of the environment's manager

        :param bool addr:
            Return only the addresses of the agents, not proxies.

        :param agent_cls:
            If specified, return only the agents that are members of the class.

        .. seealso::

            :meth:`creamas.environment.Environment.get_agents`
            :meth:`creamas.mp.EnvManager.get_agents`,
            :meth:`creamas.mp.MultiEnvironment.get_agents`
        '''
        r_manager = await self.env.connect(manager_addr, timeout=TIMEOUT)
        agents = await r_manager.get_agents(addr=addr,
                                            agent_cls=agent_cls)
        return agents

    @aiomas.expose
    async def create_connections(self, connection_map):
        '''Create connections for agents in the multi-environment.

        This is a managing function for
        :meth:`~creamas.mp.MultiEnvironment.create_connections`.
        '''
        return await self.menv.create_connections(connection_map, as_coro=True)

    @aiomas.expose
    async def get_connections(self, attitudes=True):
        '''Return connections for all the agents in the slave environments.

        This is a managing function for
        :meth:`~creamas.mp.MultiEnvironment.get_connections`.
        '''
        return await self.menv.get_connections(attitudes=attitudes)

    @aiomas.expose
    async def kill(self, addr, folder=None):
        '''Send stop command to the manager agent in a given address. This will
        shutdown the manager's environment.
        '''
        return await self.menv._kill(addr, folder)

    @aiomas.expose
    def close(self, folder=None):
        '''Implemented for consistency. This basic implementation does nothing.
        '''
        pass

    @aiomas.expose
    async def set_host_manager(self, addr, timeout=5):
        '''Set the multi-environment's manager (this agent) as a host manager
        to the manager in *addr*.

        This is a managing function for
        :py:meth:`~creamas.mp.MultiEnvironment.set_host_manager`.
        '''
        return await self.menv.set_host_manager(addr, timeout=timeout)

    @aiomas.expose
    async def trigger_all(self, *args, **kwargs):
        '''Trigger all agents in the managed multi-environment to act.

        This is a managing function for
        :py:meth:`~creamas.mp.MultiEnvironment.trigger_all`.
        '''
        rets = await self.menv.trigger_all(*args, **kwargs)
        return rets

    @aiomas.expose
    async def is_ready(self):
        '''A managing function for
        :py:meth:`~creamas.mp.MultiEnvironment.is_ready`.
        '''
        return await self.menv.is_ready()

    @aiomas.expose
    async def get_candidates(self, addr):
        '''Get candidates from the environment manager in *addr* manages.
        '''
        remote_manager = await self.env.connect(addr)
        candidates = await remote_manager.candidates()
        return candidates

    @aiomas.expose
    def add_candidate(self, artifact):
        '''Managing function for
        :meth:`~creamas.mp.MultiEnvironment.add_candidate`.
        '''
        self.menv.add_candidate(artifact)

    @aiomas.expose
    def get_votes(self, candidates):
        '''Gather votes for *candidates* from all the agents in the
        slave environments.
        '''
        self.menv._candidates = candidates
        votes = self.menv._gather_votes()
        return votes

    @aiomas.expose
    async def clear_candidates(self):
        '''Managing function for
        :meth:`~creamas.mp.MultiEnvironment.clear_candidates`.
        '''
        ret = await self.menv.clear_candidates()
        return ret

    @aiomas.expose
    async def get_artifacts(self):
        '''Get all the artifacts from the multi-environment.
        '''
        return self.menv.artifacts


class MultiEnvironment():
    '''Environment for utilizing multiple processes (and cores) on a single
    machine.

    :py:class:`MultiEnvironment` has a managing environment, typically
    containing only a single manager, and a set of slave environments each
    having their own manager and (once spawned) the actual agents.

    Currently, the implementation assumes that the slave environments do not
    use any time consuming internal initialization. If the slaves are not
    reachable after a few seconds after the initialization, an exception is
    raised. Thus, any slave environments should do their additional
    preparations, e.g. agent spawning, outside their :meth:`__init__`, after
    :py:class:`MultiEnvironment` has been initialized successfully.

    .. note::

        :py:class:`MultiEnvironment` and the slave environments are internally
        initialized to have :py:class:`aiomas.MsgPack` as the codec for the
        message serialization. Any communication to these environments and
        agents in them must use the same codec.
    '''
    def __init__(self, addr, env_cls=None, mgr_cls=None,
                 slave_addrs=[], slave_env_cls=None,
                 slave_params=None,
                 slave_mgr_cls=None, name=None, clock=None,
                 extra_ser=None, log_folder=None, log_level=logging.INFO):
        '''
        :param addr: (HOST, PORT) address for the manager environment.

        :param env_cls:
            Class for the environment. Must be a subclass of
            :py:class:`~creamas.core.environment.Environment`.

        :param mgr_cls:
            Class for the multi-environment's manager.

        :param addrs:
            List of (HOST, PORT) addresses for the slave-environments.

        :param slave_env_cls: Class for the slave environments.

        :param slave_params:
            If not None, must be a list of the same size as *addrs*. Each item
            in the list containing parameter values for one slave environment.

        :param slave_mgr_cls:
            Class of the slave environment managers.

        :param str name: Name of the environment. Will be shown in logs.
        '''
        pool, r = spawn_containers(slave_addrs, env_cls=slave_env_cls,
                                   env_params=slave_params,
                                   mgr_cls=slave_mgr_cls, codec=aiomas.MsgPack,
                                   clock=clock, extra_serializers=extra_ser)
        self._pool = pool
        self._r = r
        self._manager_addrs = ["{}{}".format(_get_base_url(a), 0) for
                               a in slave_addrs]

        self._age = 0
        self._artifacts = []
        self._candidates = []
        self._name = name if type(name) is str else 'multi-env'

        if type(log_folder) is str:
            self.logger = ObjectLogger(self, log_folder, add_name=True,
                                       init=True, log_level=log_level)
        else:
            self.logger = None

        self._addr = addr
        self._env = env_cls.create(addr, codec=aiomas.MsgPack, clock=clock,
                                   extra_serializers=extra_ser)

        if mgr_cls is not None:
            self._manager = mgr_cls(self._env)
            self._manager.menv = self
        else:
            self._manager = None

    @property
    def name(self):
        '''Name of the environment.'''
        return self._name

    @property
    def env(self):
        '''Environment hosting the manager of this multi-environment. This
        environment is also used without the manager to connect to the slave
        environment managers.
        '''
        return self._env

    async def _get_agents(self, mgr_addr, addr=True, agent_cls=None):
        r_manager = await self.env.connect(mgr_addr, timeout=TIMEOUT)
        return await r_manager.get_agents(addr=addr, agent_cls=agent_cls)

    def get_agents(self, addr=True, agent_cls=None, as_coro=False):
        '''Get agents from the slave environments.

        :param bool addr:
            If ``True``, returns only addresses of the agents, otherwise
            returns a :class:`Proxy` object for each agent.

        :param agent_cls:
            If specified, returns only agents that are members of that
            particular class.

        :param bool as_coro:
            If ``True``, returns a coroutine, otherwise runs the method in
            an event loop.

        :returns:
            A coroutine or list of :class:`Proxy` objects or addresses as
            specified by the input parameters.

        Slave environment managers are excluded from the returned list by
        default. Essentially, this method calls each slave environment
        manager's :meth:`creamas.mp.EnvManager.get_agents` asynchronously.

        .. note::

            Calling each slave environment's manager might be costly in some
            situations. Therefore, it is advisable to store the returned agent
            list if the agent sets in the slave environments are not bound to
            change.
        '''
        tasks = []
        for r_addr in self.addrs:
            t = self._get_agents(r_addr, addr=addr, agent_cls=agent_cls)
            tasks.append(asyncio.ensure_future(t))
        if as_coro:
            return util.wait_tasks(tasks)
        else:
            return aiomas.run(util.wait_tasks(tasks))

    @property
    def addrs(self):
        '''Addresses of the slave environment managers.
        '''
        return self._manager_addrs

    @property
    def manager(self):
        '''This multi-environment's master manager.
        '''
        return self._manager

    @property
    def artifacts(self):
        '''Published artifacts for all agents.'''
        return self._artifacts

    @property
    def candidates(self):
        '''Current artifact candidates, subject to e.g. agents voting to
        determine which candidate(s) are added to **artifacts**.
        '''
        return self._candidates

    async def connect(self, *args, **kwargs):
        '''Shortcut to ``self.env.connect``
        '''
        return await self.env.connect(*args, **kwargs)

    def check_ready(self):
        '''Check if this multi-environment itself is ready.

        Override in subclass if it needs any additional (asynchronous)
        initialization other than spawning its slave environments.

        :rtype: bool
        :returns: This basic implementation returns always True.
        '''
        return True

    async def is_ready(self):
        '''Check if the multi-environment has been fully initialized.

        This calls each slave environment managers' :py:meth:`is_ready` and
        checks if the multi-environment itself is ready by calling
        :py:meth:`~creamas.mp.MultiEnvironment.check_ready`.

        .. seealso::

            :py:meth:`creamas.core.environment.Environment.is_ready`
        '''
        if not self.env.is_ready():
            return False
        if not self.check_ready():
            return False
        for addr in self.addrs:
            try:
                # We have a short timeout, because this is likely to be polled
                # consecutively until the slaves are ready.
                r_manager = await self.env.connect(addr, timeout=1)
                ready = await r_manager.is_ready()
                if not ready:
                    return False
            except:
                return False
        return True

    async def wait_slaves(self, timeout, check_ready=False):
        '''Wait until all slaves are online (their managers accept connections)
        or timeout expires.

        :param int timeout:
            Timeout (in seconds) after which the method will return even though
            all the nodes are not online yet.

        :param bool check_ready:
            If ``True`` also checks if each slave environment is ready.

        Slave environment is assumed to be ready when its manager's
        :meth:`is_ready`-method returns ``True``.

        .. seealso::

            :meth:`creamas.core.environment.Environment.is_ready`,
            :meth:`creamas.mp.MultiEnvironment.is_ready`,
            :meth:`creamas.mp.EnvManager.is_ready`,
            :meth:`creamas.mp.MultiEnvManager.is_ready`

        '''
        self._log(logging.INFO, "Waiting for slaves to become ready...")
        t = time.time()
        online = []
        while len(online) < len(self.addrs):
            for addr in self.addrs:
                if time.time() - t > timeout:
                    self._log(logging.INFO, "Timeout while waiting for the "
                              "slaves to become online.")
                    return False
                if addr not in online:
                    try:
                        r_manager = await self.env.connect(addr, timeout=1)
                        ready = True
                        if check_ready:
                            ready = await r_manager.is_ready()
                        if ready:
                            online.append(addr)
                            self._log(logging.INFO, "Slave {}/{} ready: {}"
                                      .format(len(online),
                                              len(self.addrs),
                                              addr))
                    except:
                        pass
        self._log(logging.INFO, "All slaves ready in {} seconds!"
                  .format(time.time() - t))
        return True

    def _get_log_folders(self, log_folder, addrs):
        if type(log_folder) is str:
            import os
            folders = [os.path.join(log_folder, '_{}'.format(i)) for i in
                       range(len(addrs))]
        else:
            folders = [None for _ in range(len(addrs))]
            return folders

    async def set_host_manager(self, addr, timeout=TIMEOUT):
        '''Set this multi-environment's manager as the host manager for
        a manager agent in *addr*
        '''
        r_manager = await self.env.connect(addr, timeout=timeout)
        return await r_manager.set_host_manager(self.manager.addr)

    async def set_host_managers(self, timeout=5):
        '''Set the master environment's manager as host manager for the slave
        environment managers.

        :param int timeout: Timeout for connecting to the slave managers.

        This enables the slave environment managers to communicate back to the
        master environment. The master environment manager,
        :attr:`~creamas.mp.MultiEnvironment.manager`, must be an instance
        of :class:`~creamas.mp.MultiEnvManager` or its subclass if this method
        is called.
        '''
        tasks = []
        for addr in self.addrs:
            task = asyncio.ensure_future(self.set_host_manager(addr, timeout))
            tasks.append(task)
        await asyncio.gather(*tasks)

    async def trigger_act(self, addr):
        '''Trigger agent in *addr* to act.

        This method is very inefficient if used repeatedly for a large number
        of agents.

        .. seealso::

            :py:meth:`creamas.mp.MultiEnvironment.trigger_all`
        '''
        r_agent = await self.env.connect(addr, timeout=TIMEOUT)
        await r_agent.get_older()
        ret = await r_agent.act()
        return ret

    async def _trigger_slave(self, mgr_addr, *args, **kwargs):
        r_manager = await self.env.connect(mgr_addr, timeout=TIMEOUT)
        ret = await r_manager.trigger_all(*args, **kwargs)
        return ret

    async def trigger_all(self, *args, **kwargs):
        '''Trigger all agents in all the slave environments to :meth:`act`
        asynchronously.

        Given arguments and keyword arguments are passed down to each agent's
        :meth:`creamas.core.agent.CreativeAgent.act`.

        .. note::

            By design, the manager agents in each slave environment, i.e.
            :attr:`manager`, are excluded from acting.
        '''
        tasks = []
        for addr in self.addrs:
            task = asyncio.ensure_future(self._trigger_slave(addr, *args,
                                                             **kwargs))
            tasks.append(task)
        rets = await asyncio.gather(*tasks)
        rets = list(itertools.chain(*rets))
        return rets

    async def _get_smallest_env(self):
        '''Get address for the environment with smallest amount of agents.
        '''
        agents = await self._get_agents(self._manager_addrs[0])
        ns = len(agents)
        saddr = self._manager_addrs[0]
        for i, addr in enumerate(self._manager_addrs[1:]):
            agents = await self._get_agents(addr)
            n = len(agents)
            if n < ns:
                ns = n
                saddr = self._manager_addrs[i + 1]
        return saddr

    async def spawn(self, agent_cls, *args, addr=None, **kwargs):
        '''Spawn a new agent.

        If *addr* is None, spawns the agent in the slave environment with
        currently smallest number of agents.

        :param agent_cls: Subclass of :py:class:`~CreativeAgent`
        :param addr: Address for the slave enviroment's manager, if specified.
        :returns: Proxy and address for the created agent.
        '''
        if addr is None:
            addr = await self._get_smallest_env()
        r_manager = await self.env.connect(addr)
        proxy, r_addr = await r_manager.spawn(agent_cls, *args, **kwargs)
        return proxy, r_addr

    async def _clear_candidates(self, manager_addr):
        r_manager = await self.env.connect(manager_addr, timeout=TIMEOUT)
        ret = await r_manager.clear_candidates()
        return ret

    def clear_candidates(self):
        '''Remove current candidates from the environment.
        '''
        self._candidates = []
        tasks = []
        for addr in self._manager_addrs:
            tasks.append(asyncio.ensure_future(self._clear_candidates(addr)))
        aiomas.run(until=asyncio.gather(*tasks))

    async def _create_conns(self, r_addr, connection_map):
        r_manager = await self.env.connect(r_addr)
        return await r_manager.create_connections(connection_map)

    def create_connections(self, connection_map, as_coro=False):
        '''Create agent connections from the given connection map.

        :param dict connection_map:
            A map of connections to be created. Dictionary where keys are
            agent addresses and values are lists of (addr, attitude)-tuples
            suitable for
            :meth:`~creamas.core.agent.CreativeAgent.add_connections`.

        :param bool as_coro:
            If ``True`` returns a coroutine, otherwise runs the asynchronous
            calls to the slave environment managers in the event loop.

        The connection map can also include agents that are not in any slave
        environment. Only the connections from the agents that are in the slave
        environments are created.
        '''
        tasks = []
        mapped_addrs = util.addrs2managers(list(connection_map.keys()))
        for m_addr, addrs in mapped_addrs.items():
            if m_addr in self.addrs:
                cm = {}
                for ad in addrs:
                    cm[ad] = connection_map[ad]
                task = asyncio.ensure_future(self._create_conns(m_addr, cm))
                tasks.append(task)
        if as_coro:
            return util.wait_tasks(tasks)
        else:
            return aiomas.run(util.wait_tasks(tasks))

    async def _get_conns(self, r_addr, attitudes):
        r_manager = await self.env.connect(r_addr)
        return await r_manager.get_connections(attitudes)

    def get_connections(self, attitudes=True, as_coro=False):
        '''Return connections from all the agents in the slave environments.

        :param bool attitudes:
            If ``True``, returns also the attitudes for each connection.

        :param bool as_coro:
            If ``True`` returns a coroutine, otherwise runs the asynchronous
            calls to the slave environment managers in the event loop.

        .. seealso::

            :meth:`creamas.core.environment.Environment.get_connections`
        '''
        tasks = []
        for m_addr in self.addrs:
            task = asyncio.ensure_future(self._get_conns(m_addr, attitudes))
            tasks.append(task)
        if as_coro:
            return util.wait_tasks(tasks)
        else:
            return aiomas.run(util.wait_tasks(tasks))

    def add_artifact(self, artifact):
        '''Add artifact with given framing to the environment.

        :param object artifact: Artifact to be added.
        '''
        artifact.env_time = self.age
        self.artifacts.append(artifact)
        self._log(logging.DEBUG, "ARTIFACTS appended: '{}', length={}"
                  .format(artifact, len(self.artifacts)))

    def get_artifacts(self, agent):
        '''Get artifacts published by certain agent.

        :returns: All artifacts published by the agent.
        :rtype: list
        '''
        ret = [a for a in self.artifacts if agent.name == a.creator]
        return ret

    def add_candidate(self, artifact):
        '''Add candidate artifact to current candidates.
        '''
        self.candidates.append(artifact)
        self._log(logging.DEBUG, "CANDIDATES appended:'{}'"
                  .format(artifact))

    async def _validate_candidates(self, addr):
        remote_manager = await self.env.connect(addr, timeout=TIMEOUT)
        vc = remote_manager.validate_candidates(self.candidates)
        return vc

    def validate_candidates(self):
        '''Validate current candidates in the environment by pruning candidates
        that are not validated at least by one agent, i.e. they are vetoed.

        In larger societies this method might be costly, as it calls each
        agents' ``validate_candidates``-method.
        '''
        valid_candidates = set(self.candidates)
        tasks = []
        for a in self._manager_addrs:
            tasks.append(self._validate_candidates(a))
        ret = aiomas.run(until=asyncio.gather(*tasks))
        for r in ret:
            result = yield from r
            vc = set(result)
            valid_candidates = valid_candidates.intersection(vc)

        self._candidates = list(valid_candidates)
        self._log(logging.INFO,
                  "{} valid candidates after get_agents used veto."
                  .format(len(self.candidates)))

    async def get_votes(self, addr, candidates):
        '''Get votes for *candidates* from a manager in *addr*.

        Manager should implement :meth:`get_votes`.

        .. seealso::

            :meth:`creamas.mp.EnvManager.get_votes`
        '''
        r_manager = await self.env.connect(addr, timeout=TIMEOUT)
        votes = await r_manager.get_votes(candidates)
        return votes

    def _gather_votes(self):
        tasks = []
        for addr in self.addrs:
            t = asyncio.ensure_future(self.get_votes(addr, self.candidates))
            tasks.append(t)

        ret = aiomas.run(until=asyncio.gather(*tasks))
        votes = []
        for r in ret:
            votes.extend(r)
        return votes

    def perform_voting(self, method='IRV', accepted=1):
        '''Perform voting to decide the ordering of the current candidates.

        Voting calls each agent's ``vote``-method, which might be costly in
        larger societies.

        :param str method:
            Used voting method. One of the following:
            IRV = instant run-off voting,
            mean = best mean vote (requires cardinal ordering for votes),
            best = best singular vote (requires cardinal ordering, returns only
            one candidate),
            least_worst = least worst singular vote,
            random = selects random candidates

        :param int accepted:
            the number of returned candidates

        :returns:
            list of :py:class`~creamas.core.artifact.Artifact`s, accepted
            artifacts

        :rype: list
        '''
        if len(self.candidates) == 0:
            self._log(logging.WARNING, "Could not perform voting because "
                      "there are no candidates!")
            return []
        self._log(logging.INFO, "Voting from {} candidates with method: {}"
                  .format(len(self.candidates), method))

        votes = self._gather_votes()

        if method == 'IRV':
            ordering = self._vote_IRV(votes)
            best = ordering[:min(accepted, len(ordering))]
        if method == 'best':
            best = [votes[0][0]]
            for v in votes[1:]:
                if v[0][1] > best[0][1]:
                    best = [v[0]]
        if method == 'least_worst':
            best = [votes[0][-1]]
            for v in votes[1:]:
                if v[-1][1] > best[0][1]:
                    best = [v[-1]]
        if method == 'random':
            rcands = list(self.candidates)
            shuffle(rcands)
            rcands = rcands[:min(accepted, len(rcands))]
            best = [(i, 0.0) for i in rcands]
        if method == 'mean':
            best = self._vote_mean(votes, accepted)

        return best

    def add_artifacts(self, artifacts):
        '''Add artifacts to **artifacts**.

        :param artifacts:
            list of :py:class:`~creamas.core.artifact.Artifact` objects
        '''
        for artifact in artifacts:
            self.add_artifact(artifact)

    def _remove_zeros(self, votes, fpl, cl, ranking):
        '''Remove zeros in IRV voting.'''
        for v in votes:
            for r in v:
                if r not in fpl:
                    v.remove(r)
        for c in cl:
            if c not in fpl:
                if c not in ranking:
                    ranking.append((c, 0))

    def _remove_last(self, votes, fpl, cl, ranking):
        '''Remove last candidate in IRV voting.
        '''
        for v in votes:
            for r in v:
                if r == fpl[-1]:
                    v.remove(r)
        for c in cl:
            if c == fpl[-1]:
                if c not in ranking:
                    ranking.append((c, len(ranking) + 1))

    def _vote_IRV(self, votes):
        '''Perform IRV voting based on votes.
        '''
        votes = [[e[0] for e in v] for v in votes]
        f = lambda x: Counter(e[0] for e in x).most_common()
        cl = list(self.candidates)
        ranking = []
        fp = f(votes)
        fpl = [e[0] for e in fp]

        while len(fpl) > 1:
            self._remove_zeros(votes, fpl, cl, ranking)
            self._remove_last(votes, fpl, cl, ranking)
            cl = fpl[:-1]
            fp = f(votes)
            fpl = [e[0] for e in fp]

        ranking.append((fpl[0], len(ranking) + 1))
        ranking = list(reversed(ranking))
        return ranking

    def _vote_mean(self, votes, accepted):
        '''Perform mean voting based on votes.
        '''
        sums = {str(candidate): [] for candidate in self.candidates}
        for vote in votes:
            for v in vote:
                sums[str(v[0])].append(v[1])
        for s in sums:
            sums[s] = sum(sums[s]) / len(sums[s])
        ordering = list(sums.items())
        ordering.sort(key=operator.itemgetter(1), reverse=True)
        best = ordering[:min(accepted, len(ordering))]
        d = []
        for e in best:
            for c in self.candidates:
                if str(c) == e[0]:
                    d.append((c, e[1]))
        return d

    def _log(self, level, msg):
        if self.logger is not None:
            self.logger.log(level, msg)

    def save_info(self, folder, *args, **kwargs):
        '''Save information accumulated during the environments lifetime.

        Called from :py:meth:`~creamas.mp.MultiEnvironment.destroy`. Override
        in subclass.

        :param str folder: root folder to save information
        '''
        pass

    async def _kill(self, addr, folder):
        remote_manager = await self.env.connect(addr, timeout=TIMEOUT)
        ret = await remote_manager.stop(folder)
        return ret

    async def _destroy_slaves(self, folder):
        '''Shutdown the slave environments.
        '''
        rets = []
        for addr in self.addrs:
            ret = await self._kill(addr, folder)
            rets.append(ret)
        return rets

    def destroy(self, folder=None, as_coro=False):
        '''Destroy the multiprocessing environment and its slave environments.
        '''
        async def _destroy(folder):
            ret = [self.save_info(folder)]
            rets = await self._destroy_slaves(folder)
            rets = ret + rets
            # Close and join the process pool nicely.
            self._pool.close()
            self._pool.terminate()
            self._pool.join()
            await self._env.shutdown(as_coro=True)
            return rets

        if as_coro:
            return _destroy(folder)
        else:
            ret = aiomas.run(until=_destroy(folder))
            return ret


def spawn_container(addr=('localhost', 5555), env_cls=Environment,
                    mgr_cls=EnvManager, set_seed=True, *args, **kwargs):
    '''Spawn a new environment in a given address as a coroutine.

    Arguments and keyword arguments are passed down to the created environment
    at initialization time.

    If `setproctitle <https://pypi.python.org/pypi/setproctitle>`_ is
    installed, this function renames the title of the process to start with
    'creamas' so that the process is easily identifiable, e.g. with
    ``ps -x | grep creamas``.
    '''
    # Try setting the process name to easily recognize the spawned
    # environments with 'ps -x' or 'top'
    try:
        import setproctitle as spt
        title = 'creamas: {}({})'.format(env_cls.__class__.__name__,
                                         _get_base_url(addr))
        spt.setproctitle(title)
    except:
        pass

    if set_seed:
        _set_random_seeds()

    kwargs['codec'] = aiomas.MsgPack
    task = start(addr, env_cls, mgr_cls, *args, **kwargs)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(task)


def spawn_containers(addrs=[('localhost', 5555)], env_cls=Environment,
                     env_params=None,
                     mgr_cls=EnvManager, *args, **kwargs):
    '''Spawn environments in a multiprocessing :class:`multiprocessing.Pool`.

    Arguments and keyword arguments are passed down to the created environments
    at initialization time if *env_params* is None. If *env_params* is not
    None, then it is assumed to contain individual initialization parameters
    for each environment in *addrs*.

    :param addrs:
        List of (HOST, PORT) addresses for the environments.

    :param env_cls:
        Callable for the environments. Must be a subclass of
        :py:class:`~creamas.core.environment.Environment`.

    :param env_params: Initialization parameters for the environments.
    :type env_params: Iterable of same length as *addrs* or None.

    :param mgr_cls:
        Callable for the managers. Must be a subclass of
        :py:class:`~creamas.mp.EnvManager`.s

    :returns:
        The created process pool and the *ApplyAsync* results for the spawned
        environments.
    '''
    pool = multiprocessing.Pool(len(addrs))
    kwargs['env_cls'] = env_cls
    kwargs['mgr_cls'] = mgr_cls
    r = []
    for i, addr in enumerate(addrs):
        if env_params is not None:
            k = env_params[i]
            k['env_cls'] = env_cls
            k['mgr_cls'] = mgr_cls
        # Copy kwargs so that we can apply different address to different
        # containers.
        else:
            k = kwargs.copy()
        k['addr'] = addr
        ret = pool.apply_async(spawn_container, args=args, kwds=k)
        r.append(ret)
    return pool, r


@asyncio.coroutine
def start(addr, env_cls=Environment, mgr_cls=EnvManager,
          *env_args, **env_kwargs):
    """`Coroutine
    <https://docs.python.org/3/library/asyncio-task.html#coroutine>`_ that
    starts an environment with :class:`mgr_cls` manager agent.

    The agent will connect to *addr* ``('host', port)`` and wait for commands
    to spawn new agents within its environment.

    The *env_args* and *env_kwargs* will be passed to :meth:`env_cls.create()`
    factory function.

    This coroutine finishes after manager's :meth:`stop` was called or when
    a :exc:`KeyboardInterrupt` is raised.

    :param addr:
        (HOST, PORT) for the new environment

    :param env_cls:
        Class of the environment, subclass of
        :class:`~creamas.core.environment.Environment`.

    :param mgr_cls:
        Class of the manager agent, subclass of
        :class:`~creamas.mp.EnvManager`.
    """
    env_kwargs.update(as_coro=True)
    log_folder = env_kwargs.get('log_folder', None)
    env = yield from env_cls.create(addr, *env_args, **env_kwargs)
    try:
        manager = mgr_cls(env)
        env.manager = manager
        yield from manager.stop_received
    except KeyboardInterrupt:
        logger.info('Execution interrupted by user')
    finally:
        yield from env.destroy(folder=log_folder, as_coro=True)


def _set_random_seeds():
    '''Set new random seeds for the process.
    '''
    try:
        import numpy as np
        np.random.seed()
    except:
        pass

    try:
        import scipy as sp
        sp.random.seed()
    except:
        pass

    import random
    random.seed()