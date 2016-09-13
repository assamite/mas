'''
.. py:module:: spiro_agent
    :platform: Unix

Agent that creates spirographs and evaluates them by their novelty as explained
in: 

Linkola, S., Takala, T., and Toivonen, H. 2016. Novelty-Seeking Multi-Agent
Systems. In The Proceedings of The Seventh International Conference on
Computational Creativity (ICCC2016), 1-8. Paris, France. Sony CSL Paris,
France.

This implementation uses creamas.mp-module.
'''
import os
import sys
import time
from collections import Counter
import functools
import logging
import operator

import aiomas
import numpy as np
from scipy import ndimage, misc

from creamas.core import CreativeAgent, Artifact
from creamas.mp import MultiEnvironment, EnvManager, MultiEnvManager
from creamas.math import gaus_pdf

from spiro import give_dots, give_dots_yield, spiro_image

class SpiroAgent(CreativeAgent):
    '''Agent that creates spirographs and evaluates them with short term memory
    (``STMemory``) learned from previously seen spirographs.
    '''
    def __init__(self, environment, desired_novelty, search_width=10,
                 img_size=32, log_folder=None, log_level=logging.DEBUG,
                 memsize=36, learning_method='closest', learning_amount=3,
                 learn_on_add=True, veto_threshold=0.10,
                 critic_threshold=0.10, jump='none', move_radius=10.0):
        '''
        :param environment:
            The environment for the agent.

        :param desired_novelty:
            Agent's desired novelty, if maximizing novelty use -1.

        :param search_width:
            The number of new spirographs agent creates per simulation
            iteration. Defaults to 10.

        :param img_size:
            Preferred side length for the generated spirograph images. Defaults
            to 32.

        :param log_folder:
            Logging folder for the agent, if not given the logging folder is
            generated via standard means.

        :param log_level:
            Logging level for the agent. Defaults to DEBUG.

        :param memsize:
            Size of the agent's short term memory. Defaults to 36

        :param learning_method:
            Method for agent to learn from the artifacts already in the domain.
            Should be one of the following: 'closet', 'random' or 'none.
            Defaults to 'closest'.

        :param learning_amount:
            The number of the domain artifacts learned per iterations. Defaults
            to 3.

        :param learn_on_add:
            Learn new domain artifacts when they are added. Defaults to 'True'.

        :param veto_threshold:
            Threshold by which the agent rejects artifacts generated by other
            agents. Should be a value in [0, 1], values that perform well are
            in [0.06, 0.16]. Defaults to 0.10.

        :param critic_threshold:
            Threshold by which the agent rejects its own artifacts. Should be
            a value in [0, 1], values that perform well are in [0.06, 0.16].
            Defaults to 0.10.

        :param jump:
            Jump to a location of other agent's artifact if agent itself has
            not been able to generate artifact that passed its own
            ``critic_threshold`` in the last iteration. Should be either
            'random' or 'none'. Defaults to 'none'.

        :param move_radius:
            The standard deviation for agent's movement, i.e. from how large
            area new parameters for the spirograph generation are considered
            given the agent's current position in the parameter space. Defaults
            to 10.0.
        '''
        # Call first the constructor of the super class
        super().__init__(environment, log_folder=log_folder,
                         log_level=log_level)
        self.name = "{}_N{}".format(self.name, desired_novelty)
        self.spiro_args = np.random.uniform(-199, 199, [2,])
        # How many spirographs are generated to find the best one per iteration.
        self.search_width = search_width
        self.teaching_iterations = 1
        self.img_size = img_size
        self.desired_novelty = desired_novelty
        #init_func = functools.partial(np.random.normal, 0.9, 0.4)
        #self.stmem = ImageSOM(6, 6, self.img_size**2, init_func, coef=0.01)
        self.stmem = STMemory(length=memsize)
        self.env_learn_on_add = learn_on_add
        self.env_learning_method = learning_method
        self.env_learning_amount = learning_amount
        self._save_images = False
        self._novelty_threshold = veto_threshold
        self._own_threshold = critic_threshold
        self.added_last = False
        self.jump = jump
        self.move_radius = move_radius
        self.arg_history = []

    def create(self, r, r_, R=200):
        '''Create new spirograph image with given arguments. Returned image is
        scaled to agent's preferred image size.
        '''
        x, y = give_dots(R, r, r_, spins=20)
        xy = np.array([x, y]).T
        xy = np.array(np.around(xy), dtype=np.int64)
        xy = xy[(xy[:, 0] >= -250) & (xy[:, 1] >= -250) &
                (xy[:, 0] < 250) & (xy[:, 1] < 250)]
        xy = xy + 250
        img = np.ones([500, 500], dtype=np.uint8)
        img[:] = 255
        img[xy[:, 0], xy[:, 1]] = 0
        img = misc.imresize(img, [self.img_size, self.img_size])
        fimg = img / 255.0
        return fimg

    def randomize_args(self):
        '''Get new parameters for spirograph generation near agent's current
        location (*spiro_args*).
        '''
        args = self.spiro_args + np.random.normal(0, self.move_radius,
                                                  self.spiro_args.shape)
        np.clip(args, -199, 199, args)
        while args[0] == 0 or args[1] == 0:
            args = self.spiro_args + np.random.normal(0, self.move_radius,
                                                      self.spiro_args.shape)
            np.clip(args, -199, 199, args)
        return args

    def hedonic_value(self, novelty):
        '''Given the agent's desired novelty, how good the novelty value is.

        Not used if *desired_novelty*=-1
        '''
        lmax = gaus_pdf(self.desired_novelty, self.desired_novelty, 4)
        pdf = gaus_pdf(novelty, self.desired_novelty, 4)
        return pdf / lmax

    def novelty(self, img):
        '''Image's distance to the agent's short-term memory. Usually distance
        to the closest object/prototypical object model in the memory.
        '''
        dist = self.stmem.distance(img.flatten())
        return dist

    def evaluate(self, artifact):
        '''Evaluate the artifact with respect to the agents short term memory.

        Returns value in [0, 1].
        '''
        if self.desired_novelty > 0:
            return self.hedonic_value(self.novelty(artifact.obj))
        return self.novelty(artifact.obj) / self.img_size

    def invent(self, n):
        '''Invent new spirograph by taking n random steps from current position
        (spirograph generation parameters) and selecting the best one based
        on the agent's evaluation (hedonic function).

        :param int n: how many spirographs are created for evaluation
        :returns: Best created artifact.
        :rtype: :py:class:`~creamas.core.agent.Artifact`
        '''
        args = self.randomize_args()
        img = self.create(args[0], args[1])
        best_artifact = SpiroArtifact(self, img, domain='image')
        ev = self.evaluate(best_artifact)
        best_artifact.add_eval(self, ev, fr={'args': args})
        for i in range(n-1):
            args = self.randomize_args()
            img = self.create(args[0], args[1])
            artifact = SpiroArtifact(self, img, domain='image')
            ev = self.evaluate(artifact)
            artifact.add_eval(self, ev, fr={'args': args})
            if ev > best_artifact.evals[self.name]:
                best_artifact = artifact
        self.spiro_args = best_artifact.framings[self.name]['args']
        best_artifact.in_domain = False
        best_artifact.self_criticism = 'reject'
        best_artifact.creation_time = self.age
        return best_artifact

    @aiomas.expose
    def act(self):
        '''Agent's main method to create new spirographs.

        See Simulation and CreativeAgent documentation for details.
        '''
        # Learn from domain artifacts.
        self.added_last = False
        self.learn_from_domain(method=self.env_learning_method,
                               amount=self.env_learning_amount)
        # Invent new artifact
        artifact = self.invent(self.search_width)
        args = artifact.framings[self.name]['args']
        val = artifact.evals[self.name]
        self._log(logging.DEBUG, "Created spirograph with args={}, val={}"
                  .format(args, val))
        self.spiro_args = args
        #print(self.addr, self.spiro_args)
        with open(self.name, 'a') as f:
            f.write("{}\n".format(self.spiro_args))
        self.arg_history.append(self.spiro_args)
        self.add_artifact(artifact)
        if val >= self._own_threshold:
            artifact.self_criticism = 'pass'
            # Train SOM with the invented artifact
            self.learn(artifact, self.teaching_iterations)
            # Save images if logger is defined
            # Add created artifact to voting candidates in the environment
            self.env.add_candidate(artifact)
            self.added_last = True
        elif self.jump == 'random':
            largs = self.spiro_args
            self.spiro_args = np.random.uniform(-199, 199,
                                                self.spiro_args.shape)
            self._log(logging.DEBUG, "Jumped from {} to {}"
                      .format(largs, self.spiro_args))
        self.save_images(artifact)

    def learn_from_domain(self, method='random', amount=10):
        '''Learn SOM from artifacts introduced to the environment.

        :param str method:
            learning method, should be either 'random' or 'closest', where
            'random' chooses **amount** random artifacts, and 'closest' samples
            closest artifacts based on spirograph generation artifacts.
        :param int amount:
            Maximum amount of artifacts sampled
        :param bool last:
            Learn from last domain artifact in any case
        '''
        if method == 'none':
            return
        arts = self.env.artifacts
        if len(arts) == 0:
            return
        if 'random' in method:
            samples = min(len(arts), amount)
            ars = np.random.choice(arts, samples, replace=False)
            for a in ars:
                self.learn(a, self.teaching_iterations)
        if 'closest' in method:
            ars = arts
            dists = []
            for a in ars:
                args = a.framings[a.creator]['args']
                d = np.sqrt(np.sum(np.square(args - self.spiro_args)))
                dists.append((d,a))
            dists.sort(key=operator.itemgetter(0))
            for d,a in dists[:amount]:
                self.learn(a, self.teaching_iterations)

    def learn(self, spiro, iterations=1):
        '''Train short term memory with given spirograph.

        :param spiro:
            :py:class:`SpiroArtifact` object
        '''
        for i in range(iterations):
            self.stmem.train_cycle(spiro.obj.flatten())

    def domain_artifact_added(self, spiro, iterations=1):
        if spiro.creator == self.name:
            for a in self.A:
                if a == spiro:
                    a.in_domain = True
                    self.save_images(a)
        if self.env_learn_on_add:
            self.learn(spiro)

    def validate_candidates(self, candidates):
        besteval = 0.0
        bestcand = None
        valid = []
        for c in candidates:
            if c.creator != self.name:
                ceval= self.evaluate(c)
                if ceval >= self._novelty_threshold:
                    valid.append(c)
                    if ceval > besteval:
                        besteval = ceval
                        bestcand = c
            else:
                valid.append(c)
        if self.jump == 'best':
            if bestcand is not None and not self.added_last:
                largs = self.spiro_args
                self.spiro_args = bestcand.framings[bestcand.creator]['args']
                self._log(logging.INFO,
                          "Jumped from {} to {}".format(largs, self.spiro_args))
        return valid

    def save_images(self, artifact):
        if not self._save_images:
            return
        img = artifact.obj
        sc = artifact.self_criticism
        domain = artifact.in_domain
        ctime = artifact.creation_time
        if self.logger is not None:
            im_name = '{}_N{}_{:0>4}_sc={}_d={}.png'.format(self.name, self.desired_novelty,
                                                    ctime, sc, domain)
            path = os.path.join(self.logger.folder, im_name)
            misc.imsave(path, img)

    def _artifact_distances(self):
        accepted = [a for a in self.A if a.self_criticism == 'pass']
        accepted = sorted(accepted, key=lambda x: x.creation_time)
        distances = []
        indeces = []
        for i,a1 in enumerate(accepted[1:]):
            spiro1 = a1.obj
            j = i+1
            mdist = np.sqrt(spiro1.flatten().shape[0])
            for a2 in accepted[:j]:
                spiro2 = a2.obj
                dist = np.sqrt(np.sum(np.square(spiro1.flatten() - spiro2.flatten())))
                if dist < mdist:
                    mdist = dist
            distances.append(mdist)
            indeces.append(i)
        mean_dist = np.mean(distances)
        return mean_dist, distances, indeces

    def plot_distances(self, mean_dist, distances, indeces):
        '''Plot distances of the generated spirographs w.r.t. the previously
        generated spirogaphs.
        '''
        from matplotlib import pyplot as plt
        x = np.arange(len(distances))
        y = [mean_dist for i in x]
        fig, ax = plt.subplots()
        data_line = ax.plot(indeces, distances, label='Min Distance to previous',
                        marker='.', color='black', linestyle="")
        mean_line = ax.plot(indeces, y, label='Mean', linestyle='--', color='green')
        if len(distances) > 0:
            z = np.poly1d(np.polyfit(x,distances,2))
            f = [z(i) for i in x]
            mean_line = ax.plot(indeces, f, label='Fitted', linestyle='-', color='red')
        legend = ax.legend(loc='upper right', prop={'size':8})
        agent_vars = "{}_{}_{}{}_last={}_stmem=list{}_veto={}_sc={}_jump={}_sw={}_mr={}_maxN".format(
            self.name, self.age, self.env_learning_method, self.env_learning_amount, self.env_learn_on_add,
            self.stmem.length, self._novelty_threshold, self._own_threshold,
            self.jump, self.search_width, self.move_radius)
        ax.set_title("{} min distances: env_learn={} {}"
                     .format(self.name, self.env_learning_method,
                             self.env_learning_amount))
        ax.set_ylabel('min distance to preceding artifact')
        ax.set_xlabel('iteration')
        if self.logger is not None:
            imname = os.path.join(self.logger.folder, '{}_dists.png'.format(agent_vars))
            plt.savefig(imname)
            plt.close()
        else:
            plt.show()

    def plot_places(self):
        '''Plot places where the agent has been and generated a spirograph.
        '''
        from matplotlib import pyplot as plt
        fig, ax = plt.subplots()
        x = []
        y = []

        if len(self.arg_history) > 1:
            xs = []
            ys = []
            for p in self.arg_history:
                xs.append(p[0])
                ys.append(p[1])
            ax.plot(xs, ys, color=(0.0, 0.0, 1.0, 0.1))

        for a in self.A:
            if a.self_criticism == 'pass':
                args = a.framings[a.creator]['args']
                x.append(args[0])
                y.append(args[1])

        sc = ax.scatter(x, y, marker="x", color='red')
        ax.set_xlim([-200, 200])
        ax.set_ylim([-200, 200])

        agent_vars = "{}_{}_{}{}_last={}_stmem=list{}_veto={}_sc={}_jump={}_sw={}_mr={}_maxN".format(
            self.name, self.age, self.env_learning_method, self.env_learning_amount, self.env_learn_on_add,
            self.stmem.length, self._novelty_threshold, self._own_threshold,
            self.jump, self.search_width, self.move_radius)

        if self.logger is not None:
            imname = os.path.join(self.logger.folder, '{}.png'.format(agent_vars))
            plt.savefig(imname)
            plt.close()

            fname = os.path.join(self.logger.folder, '{}.txt'.format(agent_vars))
            with open(fname, "w") as f:
                f.write(" ".join([str(e) for e in xs]))
                f.write("\n")
                f.write(" ".join([str(e) for e in ys]))
                f.write("\n")
                f.write(" ".join([str(e) for e in x]))
                f.write("\n")
                f.write(" ".join([str(e) for e in y]))
                f.write("\n")
        else:
            plt.show()

    @aiomas.expose
    def close(self, folder):
        mean_dist, dists, indeces = self._artifact_distances()
        if len(dists) == 0:
            mean_dist = 0.0
        self._log(logging.INFO, "Mean of distances: {}".format(mean_dist))
        self.plot_distances(mean_dist, dists, indeces)
        self.plot_places()
        return mean_dist


class SpiroArtifact(Artifact):
    '''Artifact class for Spirographs.
    '''
    def __str__(self):
        return "Spirograph by:{} {}".format(self.creator,
                                            self.framings[self.creator])

    def __repr__(self):
        return self.__str__()

    def __lt__(self, other):
        return str(self) < str(other)

class SpiroEnvManager(EnvManager):
    @aiomas.expose
    async def candidates(self):
        return self.container.candidates

    @aiomas.expose
    async def add_candidate(self, artifact):
        host_manager = await self.container.connect(self._host_addr)
        host_manager.add_candidate(artifact)


class SpiroMultiEnvManager(MultiEnvManager):
    @aiomas.expose
    def add_candidate(self, artifact):
        self.container.add_candidate(artifact)

    async def get_candidates(self, addr):
        print(addr)
        remote_manager = await self.container.connect(addr)
        candidates = await remote_manager.candidates()
        return candidates


class SpiroMultiEnvironment(MultiEnvironment):
    '''MultiEnvironment for agents creating spirographs.
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_image_number = 1
        self.img_size = 32
        self.age = 0
        self.voting_method = 'mean'
        self.valid_cand = []
        self.suggested_cand = []

    async def get_candidates(self, addr):
        candidates =  await self._manager.get_candidates(addr)
        return candidates

    async def gather_candidates(self):
        cands = []
        for addr in self._manager_addrs:
            cand = await self.get_candidates(addr)
            cands.extend(cand)
        return cands

    def vote_and_save_info(self, age):
        self.age = age
        self._candidates = aiomas.run(until=self.gather_candidates())
        print(len(self.candidates))
        self.suggested_cand.append(len(self.candidates))
        self.validate_candidates()
        self.valid_cand.append(len(self.candidates))
        artifacts = self.perform_voting(method=self.voting_method)
        threshold = 0.0

        for a,v in artifacts:
            accepted = True if v >= threshold else False
            a.accepted = accepted
            self.add_artifact(a)
            for agent in self.get_agents():
                agent.domain_artifact_added(a)

        self.clear_candidates()
        self.valid_candidates = []

    def _calc_distances(self):
        accepted_x = []
        accepted_y = []
        rejected_x = []
        rejected_y = []
        sort_arts = sorted(self.artifacts, key=lambda x: x.env_time)

        distances = []
        for i,a1 in enumerate(sort_arts[1:]):
            spiro1 = a1.obj
            i = i+1
            mdist = np.sqrt(spiro1.flatten().shape[0])
            for a2 in sort_arts[:i]:
                spiro2 = a2.obj
                dist = np.sqrt(np.sum(np.square(spiro1.flatten() - spiro2.flatten())))
                if dist < mdist:
                    mdist = dist
            if a1.accepted:
                accepted_x.append(a1.env_time)
                accepted_y.append(mdist)
            else:
                rejected_x.append(a1.env_time)
                rejected_y.append(mdist)
        mean_dist = np.mean(accepted_y)
        self._log(logging.INFO, "Mean of (accepted) distances: {}".format(mean_dist))
        return mean_dist, (accepted_x, accepted_y), (rejected_x, rejected_y)

    def save_info(self, folder, ameans):
        mean_dist, accs, rejs = self._calc_distances()
        fitted_curve = None
        axs, adists = accs
        if len(axs) > 0:
            fitted_curve = np.poly1d(np.polyfit(axs, adists, 2))
        self.plot_distances(ameans, accs, rejs, fitted_curve)
        self.plot_creators()
        self.plot_places()
        mean_sug_cand = np.mean(self.suggested_cand)
        mean_valid_cand = np.mean(self.valid_cand)
        return mean_dist, accs, rejs, mean_sug_cand, mean_valid_cand

    def plot_creators(self):
        from matplotlib import pyplot as plt
        counter = Counter([a.creator for a in self.artifacts])
        ticks = np.arange(len(counter.values()))
        c = list(counter.items())
        c.sort(key=operator.itemgetter(0))
        labels = [e[0] for e in c]
        counts = [e[1] for e in c]
        fig, ax = plt.subplots()
        rects1 = ax.bar(ticks, counts, color='green')
        ax.set_ylabel('env artifacts')
        ax.set_title('Number of environment artifacts per agent')
        ax.set_xticks(ticks + 0.5)
        ax.set_xticklabels(labels)
        plt.xticks(rotation=90)

        if self.logger is not None:
            imname = os.path.join(self.logger.folder, 'env_a#_a{}_i{}_v{}'
                                  .format(len(self.get_agents()), self.age,
                                          self.voting_method))
            plt.savefig(imname)
            plt.close()
        else:
            plt.show()


    def plot_distances(self, ameans, accs, rejs, fitted_curve=None):
        from matplotlib import pyplot as plt
        title="Minimum distance to preceding domain artifact ({} env artifacts)".format(len(self.artifacts))
        vxs = np.arange(1, self.age+1)

        axs, adists = accs
        rxs, rdists = rejs
        mean_dist = np.mean(adists)
        y = [mean_dist for i in vxs]
        amin = [ameans[0] for i in vxs]
        amax = [ameans[1] for i in vxs]
        amean = [ameans[2] for i in vxs]

        fig, ax = plt.subplots()
        data_line = ax.plot(axs, adists, label='accepted artifact',
                            marker='.', color='black', linestyle="")
        if len(rxs) > 0: # if there are rejected artifacts, plot them
            no_line = ax.plot(rxs, rdists, label='rejected artifact',
                              marker='x', color='red', linestyle="")
        mean_line = ax.plot(vxs, y, label='domain mean distance', linestyle='--', color='green')
        mean_line = ax.plot(vxs, amin, label='agent min mean', linestyle='-.', color='magenta')
        mean_line = ax.plot(vxs, amean, label='agent mean', linestyle='--', color='magenta')
        mean_line = ax.plot(vxs, amax, label='agent max mean', linestyle=':', color='magenta')
        if fitted_curve is not None:
            f = [fitted_curve(i) for i in axs]
            fitted_line = ax.plot(axs, f, label='Fitted', linestyle='-', color='red')
        legend = ax.legend(loc='upper right', prop={'size':8})
        ax.set_title(title)
        ax.set_ylabel('min distance to preceding env artifact')
        ax.set_xlabel('iteration')

        ax2 = ax.twinx()
        valid_line = ax2.plot(vxs, self.valid_cand, color='cornflowerblue',
                              marker="x", linestyle="")
        ax2.set_ylabel('valid candidates after veto', color='cornflowerblue')
        a = self.get_agents()[0]
        agent_vars = "{}{}_last={}_stmem=list{}_veto={}_sc={}_jump={}_sw={}_mr={}_mean={}_amean={}_maxN".format(
            a.env_learning_method, a.env_learning_amount, a.env_learn_on_add, a.stmem.length,
            a._novelty_threshold, a._own_threshold, a.jump, a.search_width, a.move_radius,
            mean_dist, ameans[2])

        if self.logger is not None:
            imname = os.path.join(self.logger.folder, 'env_a{}_i{}_v{}_{}.png'
                                  .format(len(self.get_agents()), self.age,
                                          self.voting_method, agent_vars))
            plt.savefig(imname)
            plt.close()
        else:
            plt.show()

    def plot_places(self):
        '''Plot places (in the parameter space) of all the generated artifacts
        and the artifacts accepted to the domain.
        '''
        from matplotlib import pyplot as plt
        fig, ax = plt.subplots()
        title = "Agent places, artifacts and env artifacts ({} env artifacts)".format(len(self.artifacts))

        x = []
        y = []
        for a in self.get_agents():
            args = a.arg_history
            x = x + [e[0] for e in args]
            y = y + [e[1] for e in args]
        sc = ax.scatter(x, y, marker='.', color=(0, 0, 1, 0.1), label='agent place')

        x = []
        y = []
        for a in self.get_agents():
            arts = a.A
            for ar in arts:
                if ar.self_criticism == 'pass':
                    args = ar.framings[ar.creator]['args']
                    x.append(args[0])
                    y.append(args[1])
        sc = ax.scatter(x, y, marker="x", color=(0, 0, 1, 0.3), label='agent artifact')

        x = []
        y = []
        for a in self.artifacts:
            args = a.framings[a.creator]['args']
            x.append(args[0])
            y.append(args[1])

        sc = ax.scatter(x, y, marker="x", color='red', label='env artifact',
                        s=40)
        ax.set_xlim([-200, 200])
        ax.set_ylim([-200, 200])
        ax.set_xlabel('r')
        ax.set_ylabel('r_')
        ax.legend(loc='center left', bbox_to_anchor=(1, 0.5), fontsize=10)
        ax.set_title(title)

        a = self.get_agents()[0]
        agent_vars = "{}{}_last={}, stmem=list{}_veto={}_sc={}_jump={}_sw={}_mr={}_maxN".format(
            a.env_learning_method, a.env_learning_amount, a.env_learn_on_add, a.stmem.length,
            a._novelty_threshold, a._own_threshold, a.jump, a.search_width, a.move_radius)
        plt.tight_layout(rect=(0,0,0.8,1))

        if self.logger is not None:
            imname = os.path.join(self.logger.folder, 'arts_a{}_i{}_v{}_{}.png'
                                  .format(len(self.get_agents()), self.age,
                                          self.voting_method, agent_vars))
            plt.savefig(imname)
            plt.close()
        else:
            plt.show()
'''
    def destroy(self, folder):
        ameans = []
        for a in self.get_agents():
            remote_agent = self._manager.container.connect(a)
            md = remote_agent.close(folder=folder)
            ameans.append(md)
        amin = min(ameans)
        amax = max(ameans)
        amean = np.mean(ameans)
        a = self.get_agents()[0]
        ret = self.save_info(folder, [amin, amax, amean])
        agent_vars = "{}{}_last={}_veto={}_sc={}_jump={}_stmem=list{}_sw={}_mr={}_maxN".format(
            a.env_learning_method, a.env_learning_amount, a.env_learn_on_add,
            a._novelty_threshold, a._own_threshold, a.jump,
            a.stmem.length, a.search_width, a.move_radius)

        if self.logger is not None:
            fname = os.path.join(self.logger.folder, 'runinfo_a{}_i{}_v{}_{}.txt'
                                  .format(len(self.get_agents()), self.age,
                                          self.voting_method, agent_vars))
            with open(fname, "w") as f:
                for e in ret:
                    f.write("{}\n".format(e))
                f.write("amin:{}\n".format(amin))
                f.write("amax:{}\n".format(amax))
                f.write("amean:{}\n".format(amean))
        self.shutdown()
        ret = ret + ((amin,amax, amean),)
        return ret
'''

class STMemory():
    '''Agent's short-term memory model using a simple list which stores
    artifacts as is.'''
    def __init__(self, length):
        self.length = length
        self.artifacts = []

    def _add_artifact(self, artifact):
        if len(self.artifacts) == self.length:
            self.artifacts = self.artifacts[:-1]
        self.artifacts.insert(0, artifact)

    def learn(self, artifact):
        '''Learn new artifact. Removes last artifact from the memory if it is
        full.'''
        self._add_artifact(artifact)

    def train_cycle(self, artifact):
        '''Train cycle method to keep the interfaces the same with the SOM
        implementation of the short term memory.
        '''
        self.learn(artifact)

    def distance(self, artifact):
        mdist = np.sqrt(artifact.shape[0])
        if len(self.artifacts) == 0:
            return np.random.random()*mdist
        for a in self.artifacts:
            d = np.sqrt(np.sum(np.square(a - artifact)))
            if d < mdist:
                mdist = d
        return mdist


if __name__ == "__main__":
    import asyncio
    import aiomas
    from creamas.core import Simulation, Environment
    from matplotlib import pyplot as plt

    addr = ('localhost', 5550)
    addrs = [('localhost', 5555),
             ('localhost', 5556),
             ('localhost', 5557),
             ('localhost', 5558)]

    log_folder = 'logs'
    menv = SpiroMultiEnvironment(addr, mgr_cls=SpiroMultiEnvManager,
                                slave_env_cls=Environment,
                                slave_mgr_cls=SpiroEnvManager,
                                slave_addrs=addrs, log_folder=log_folder)
    menv.log_folder = log_folder
    for _ in range(4):
        ret = aiomas.run(until=asyncio.ensure_future(menv.spawn('spiro_agent_mp:SpiroAgent', addr='tcp://localhost:5555/0', desired_novelty=-1, log_folder=log_folder)))
    time.sleep(4)
    for _ in range(4):
        ret = aiomas.run(until=asyncio.ensure_future(menv.spawn('spiro_agent_mp:SpiroAgent', addr='tcp://localhost:5556/0', desired_novelty=-1, log_folder=log_folder)))

#    for _ in range(16):
#        ret = aiomas.run(until=menv.spawn('spiro_agent_mp:SpiroAgent', desired_novelty=-1, log_folder=log_folder))
    #    print(ret)

    sim = Simulation(menv, log_folder=log_folder,
                     callback=menv.vote_and_save_info)
    #time.sleep(10)
    sim.steps(20)
    ret = sim.end()
    print(ret)
    
