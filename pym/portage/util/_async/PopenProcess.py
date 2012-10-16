# Copyright 2012 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

from _emerge.SubProcess import SubProcess

class PopenProcess(SubProcess):

	__slots__ = ("pipe_reader", "proc",)

	def __init__(self, **kwargs):
		SubProcess.__init__(self, **kwargs)
		self.pid = self.proc.pid
		self._registered = True

	def _start(self):
		if self.pipe_reader is not None:
			self.pipe_reader.addExitListener(self._pipe_reader_exit)
			self.pipe_reader.start()

	def _pipe_reader_exit(self, pipe_reader):
		self._reg_id = self.scheduler.child_watch_add(
			self.pid, self._child_watch_cb)

	def _child_watch_cb(self, pid, condition, user_data=None):
		self._reg_id = None
		self._waitpid_cb(pid, condition)
		self.wait()
