"""The locks: one holder at a time, a wait that gives up, and a holder that died never blocks anyone."""

import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sessionlift import desktop
from sessionlift.locks import FileLock, LockTimeout
from sessionlift.moves import RepoLock

from fakes import _rmtree


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sessionlift-locks-"))
        self.addCleanup(_rmtree, self.tmp)

    def test_file_lock_is_exclusive_and_waits(self):
        path = self.tmp / "state" / "x.lock"
        with FileLock(path, timeout=1):
            started = time.time()
            with self.assertRaises(LockTimeout):
                with FileLock(path, timeout=0.3):
                    pass
            self.assertGreaterEqual(time.time() - started, 0.25)
        with FileLock(path, timeout=0.3):  # free again
            pass

    def test_a_waiter_gets_the_lock_once_it_is_free(self):
        path = self.tmp / "y.lock"
        got = []
        holder = FileLock(path, timeout=1).__enter__()

        def wait():
            with FileLock(path, timeout=5):
                got.append(time.time())

        t = threading.Thread(target=wait)
        t.start()
        time.sleep(0.3)
        released = time.time()
        holder.__exit__(None, None, None)
        t.join(5)
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0], released)

    def test_repo_lock_is_reentrant_and_frees_a_dead_holders_file(self):
        path = self.tmp / "git-demo.lock"
        path.write_text("999999999 0")  # a process that is not running
        self.assertFalse(desktop.stamp_alive(path.read_text()))
        lock = RepoLock(path, timeout=2)
        with lock:
            self.assertEqual(path.read_text(), desktop.pid_stamp())
            with lock:
                self.assertTrue(path.exists())
            self.assertTrue(path.exists(), "still held by the outer block")
        self.assertFalse(path.exists())

    def test_repo_lock_goes_on_without_a_lock_held_too_long(self):
        path = self.tmp / "git-demo.lock"
        path.write_text(desktop.pid_stamp())  # held by a live process (this one, from elsewhere)
        started, said = time.time(), io.StringIO()
        with contextlib.redirect_stdout(said), RepoLock(path, timeout=0.5):
            self.assertGreaterEqual(time.time() - started, 0.4)
        self.assertIn("going on without it", said.getvalue())
        self.assertTrue(path.exists(), "a lock it never got is not its to remove")

    def test_pid_stamps(self):
        self.assertTrue(desktop.stamp_alive(desktop.pid_stamp()))
        self.assertTrue(desktop.stamp_alive(str(os.getpid())), "a bare pid is accepted")
        self.assertFalse(desktop.stamp_alive(""))
        self.assertFalse(desktop.stamp_alive("not a pid"))
        self.assertTrue(desktop.process_alive(os.getpid(), "2026-10-13T10:00:00Z"),
                        "a start time in another form does not break the check")


if __name__ == "__main__":
    unittest.main()
