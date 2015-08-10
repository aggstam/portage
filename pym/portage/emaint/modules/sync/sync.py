# Copyright 2014-2015 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

import logging
import os
import sys

import portage
portage._internal_caller = True
portage._sync_mode = True
from portage.localization import _
from portage.output import bold, red, create_color_func
from portage._global_updates import _global_updates
from portage.sync.controller import SyncManager
from portage.util import writemsg_level
from portage.util.digraph import digraph
from portage.util._async.AsyncScheduler import AsyncScheduler
from portage.util._eventloop.global_event_loop import global_event_loop
from portage.util._eventloop.EventLoop import EventLoop

import _emerge
from _emerge.emergelog import emergelog


portage.proxy.lazyimport.lazyimport(globals(),
	'_emerge.actions:adjust_configs,load_emerge_config',
	'_emerge.chk_updated_cfg_files:chk_updated_cfg_files',
	'_emerge.main:parse_opts',
	'_emerge.post_emerge:display_news_notification',
)

warn = create_color_func("WARN")

if sys.hexversion >= 0x3000000:
	_basestring = str
else:
	_basestring = basestring


class SyncRepos(object):

	short_desc = "Check repos.conf settings and/or sync repositories"

	@staticmethod
	def name():
		return "sync"


	def can_progressbar(self, func):
		return False


	def __init__(self, emerge_config=None, emerge_logging=False):
		'''Class init function

		@param emerge_config: optional an emerge_config instance to use
		@param emerge_logging: boolean, defaults to False
		'''
		if emerge_config is None:
			# need a basic options instance
			actions, opts, _files = parse_opts([], silent=True)
			emerge_config = load_emerge_config(
				action='sync', args=_files, opts=opts)

			# Parse EMERGE_DEFAULT_OPTS, for settings like
			# --package-moves=n.
			cmdline = portage.util.shlex_split(
				emerge_config.target_config.settings.get(
				"EMERGE_DEFAULT_OPTS", ""))
			emerge_config.opts = parse_opts(cmdline, silent=True)[1]

			if hasattr(portage, 'settings'):
				# cleanly destroy global objects
				portage._reset_legacy_globals()
				# update redundant global variables, for consistency
				# and in order to conserve memory
				portage.settings = emerge_config.target_config.settings
				portage.db = emerge_config.trees
				portage.root = portage.db._target_eroot

		self.emerge_config = emerge_config
		if emerge_logging:
			_emerge.emergelog._disable = False
		self.xterm_titles = "notitles" not in \
			self.emerge_config.target_config.settings.features
		emergelog(self.xterm_titles, " === sync")


	def auto_sync(self, **kwargs):
		'''Sync auto-sync enabled repos'''
		options = kwargs.get('options', None)
		selected = self._get_repos(True)
		if options:
			return_messages = options.get('return-messages', False)
		else:
			return_messages = False
		return self._sync(selected, return_messages,
			emaint_opts=options)


	def all_repos(self, **kwargs):
		'''Sync all repos defined in repos.conf'''
		selected = self._get_repos(auto_sync_only=False)
		options = kwargs.get('options', None)
		if options:
			return_messages = options.get('return-messages', False)
		else:
			return_messages = False
		return self._sync(selected, return_messages,
			emaint_opts=options)


	def repo(self, **kwargs):
		'''Sync the specified repo'''
		options = kwargs.get('options', None)
		if options:
			repos = options.get('repo', '')
			return_messages = options.get('return-messages', False)
		else:
			return_messages = False
		if isinstance(repos, _basestring):
			repos = repos.split()
		available = self._get_repos(auto_sync_only=False)
		selected = self._match_repos(repos, available)
		if not selected:
			msgs = [red(" * ") + "Emaint sync, The specified repos were not found: %s"
				% (bold(", ".join(repos))) + "\n   ...returning"
				]
			if return_messages:
				return msgs
			return
		return self._sync(selected, return_messages,
			emaint_opts=options)


	@staticmethod
	def _match_repos(repos, available):
		'''Internal search, matches up the repo.name in repos

		@param repos: list, of repo names to match
		@param avalable: list of repo objects to search
		@return: list of repo objects that match
		'''
		selected = []
		for repo in available:
			if repo.name in repos:
				selected.append(repo)
		return selected


	def _get_repos(self, auto_sync_only=True):
		selected_repos = []
		unknown_repo_names = []
		missing_sync_type = []
		if self.emerge_config.args:
			for repo_name in self.emerge_config.args:
				#print("_get_repos(): repo_name =", repo_name)
				try:
					repo = self.emerge_config.target_config.settings.repositories[repo_name]
				except KeyError:
					unknown_repo_names.append(repo_name)
				else:
					selected_repos.append(repo)
					if repo.sync_type is None:
						missing_sync_type.append(repo)

			if unknown_repo_names:
				writemsg_level("!!! %s\n" % _("Unknown repo(s): %s") %
					" ".join(unknown_repo_names),
					level=logging.ERROR, noiselevel=-1)

			if missing_sync_type:
				writemsg_level("!!! %s\n" %
					_("Missing sync-type for repo(s): %s") %
					" ".join(repo.name for repo in missing_sync_type),
					level=logging.ERROR, noiselevel=-1)

			if unknown_repo_names or missing_sync_type:
				writemsg_level("Missing or unknown repos... returning",
					level=logging.INFO, noiselevel=2)
				return []

		else:
			selected_repos.extend(self.emerge_config.target_config.settings.repositories)
		#print("_get_repos(), selected =", selected_repos)
		if auto_sync_only:
			return self._filter_auto(selected_repos)
		return selected_repos


	def _filter_auto(self, repos):
		selected = []
		for repo in repos:
			if repo.auto_sync in ['yes', 'true']:
				selected.append(repo)
		return selected


	def _sync(self, selected_repos, return_messages,
		emaint_opts=None):

		if emaint_opts is not None:
			for k, v in emaint_opts.items():
				if v is not None:
					k = "--" + k.replace("_", "-")
					self.emerge_config.opts[k] = v

		selected_repos = [repo for repo in selected_repos if repo.sync_type is not None]
		msgs = []
		if not selected_repos:
			msgs.append("Emaint sync, nothing to sync... returning")
			if return_messages:
				msgs.extend(self.rmessage([('None', os.EX_OK)], 'sync'))
				return msgs
			return
		# Portage needs to ensure a sane umask for the files it creates.
		os.umask(0o22)

		sync_manager = SyncManager(
			self.emerge_config.target_config.settings, emergelog)

		max_jobs = (self.emerge_config.opts.get('--jobs', 1)
			if 'parallel-fetch' in self.emerge_config.
			target_config.settings.features else 1)
		sync_scheduler = SyncScheduler(emerge_config=self.emerge_config,
			selected_repos=selected_repos, sync_manager=sync_manager,
			max_jobs=max_jobs,
			event_loop=global_event_loop() if portage._internal_caller else
				EventLoop(main=False))

		sync_scheduler.start()
		sync_scheduler.wait()
		retvals = sync_scheduler.retvals
		msgs.extend(sync_scheduler.msgs)

		# Reload the whole config.
		portage._sync_mode = False
		self._reload_config()
		self._do_pkg_moves()
		msgs.extend(self._check_updates())
		display_news_notification(self.emerge_config.target_config,
			self.emerge_config.opts)
		# run the post_sync_hook one last time for
		# run only at sync completion hooks
		rcode = sync_manager.perform_post_sync_hook('')
		if retvals:
			msgs.extend(self.rmessage(retvals, 'sync'))
		else:
			msgs.extend(self.rmessage([('None', os.EX_OK)], 'sync'))
		if rcode:
			msgs.extend(self.rmessage([('None', rcode)], 'post-sync'))
		if return_messages:
			return msgs
		return


	def _do_pkg_moves(self):
		if self.emerge_config.opts.get('--package-moves') != 'n' and \
			_global_updates(self.emerge_config.trees,
			self.emerge_config.target_config.mtimedb["updates"],
			quiet=("--quiet" in self.emerge_config.opts)):
			self.emerge_config.target_config.mtimedb.commit()
			# Reload the whole config.
			self._reload_config()


	def _check_updates(self):
		mybestpv = self.emerge_config.target_config.trees['porttree'].dbapi.xmatch(
			"bestmatch-visible", portage.const.PORTAGE_PACKAGE_ATOM)
		mypvs = portage.best(
			self.emerge_config.target_config.trees['vartree'].dbapi.match(
				portage.const.PORTAGE_PACKAGE_ATOM))

		chk_updated_cfg_files(self.emerge_config.target_config.root,
			portage.util.shlex_split(
				self.emerge_config.target_config.settings.get("CONFIG_PROTECT", "")))

		msgs = []
		if mybestpv != mypvs and "--quiet" not in self.emerge_config.opts:
			msgs.append('')
			msgs.append(warn(" * ")+bold("An update to portage is available.")+" It is _highly_ recommended")
			msgs.append(warn(" * ")+"that you update portage now, before any other packages are updated.")
			msgs.append('')
			msgs.append(warn(" * ")+"To update portage, run 'emerge --oneshot portage' now.")
			msgs.append('')
		return msgs


	def _reload_config(self):
		'''Reload the whole config from scratch.'''
		load_emerge_config(emerge_config=self.emerge_config)
		adjust_configs(self.emerge_config.opts, self.emerge_config.trees)


	def rmessage(self, rvals, action):
		'''Creates emaint style messages to return to the task handler'''
		messages = []
		for rval in rvals:
			messages.append("Action: %s for repo: %s, returned code = %s"
				% (action, rval[0], rval[1]))
		return messages


class SyncScheduler(AsyncScheduler):
	'''
	Sync repos in parallel, but don't sync a given repo until all
	of its masters have synced.
	'''
	def __init__(self, **kwargs):
		'''
		@param emerge_config: an emerge_config instance
		@param selected_repos: list of RepoConfig instances
		@param sync_manager: a SyncManger instance
		'''
		self._emerge_config = kwargs.pop('emerge_config')
		self._selected_repos = kwargs.pop('selected_repos')
		self._sync_manager = kwargs.pop('sync_manager')
		AsyncScheduler.__init__(self, **kwargs)
		self._init_graph()
		self.retvals = []
		self.msgs = []

	def _init_graph(self):
		'''
		Graph relationships between repos and their masters.
		'''
		self._sync_graph = digraph()
		self._leaf_nodes = []
		self._repo_map = {}
		self._running_repos = set()
		for repo in self._selected_repos:
			self._repo_map[repo.name] = repo
			self._sync_graph.add(repo.name, None)
			for master in repo.masters:
				self._repo_map[master.name] = master
				self._sync_graph.add(master.name, repo.name)
		self._update_leaf_nodes()

	def _task_exit(self, task):
		'''
		Remove the task from the graph, in order to expose
		more leaf nodes.
		'''
		self._running_tasks.discard(task)
		returncode = task.returncode
		if task.returncode == os.EX_OK:
			returncode, message, updatecache_flg = task.result
			if message:
				self.msgs.append(message)
		repo = task.kwargs['repo'].name
		self._running_repos.remove(repo)
		self.retvals.append((repo, returncode))
		self._sync_graph.remove(repo)
		self._update_leaf_nodes()
		super(SyncScheduler, self)._task_exit(self)

	def _update_leaf_nodes(self):
		'''
		Populate self._leaf_nodes with current leaves from
		self._sync_graph. If a circular master relationship
		is discovered, choose a random node to break the cycle.
		'''
		if self._sync_graph and not self._leaf_nodes:
			self._leaf_nodes = [obj for obj in
				self._sync_graph.leaf_nodes()
				if obj not in self._running_repos]

			if not (self._leaf_nodes or self._running_repos):
				# If there is a circular master relationship,
				# choose a random node to break the cycle.
				self._leaf_nodes = [next(iter(self._sync_graph))]

	def _next_task(self):
		'''
		Return a task for the next available leaf node.
		'''
		if not self._sync_graph:
			raise StopIteration()
		# If self._sync_graph is non-empty, then self._leaf_nodes
		# is guaranteed to be non-empty, since otherwise
		# _can_add_job would have returned False and prevented
		# _next_task from being immediately called.
		node = self._leaf_nodes.pop()
		self._running_repos.add(node)
		self._update_leaf_nodes()

		task = self._sync_manager.async(
			self._emerge_config, self._repo_map[node])
		return task

	def _can_add_job(self):
		'''
		Returns False if there are no leaf nodes available.
		'''
		if not AsyncScheduler._can_add_job(self):
			return False
		return bool(self._leaf_nodes) and not self._terminated.is_set()

	def _keep_scheduling(self):
		'''
		Schedule as long as the graph is non-empty, and we haven't
		been terminated.
		'''
		return bool(self._sync_graph) and not self._terminated.is_set()
