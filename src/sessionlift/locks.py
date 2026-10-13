"""An exclusive lock on a file, waited for up to a timeout: flock on Linux (the server), msvcrt on
Windows (where the tests also run the server side). The lock goes with the process, so a process that
dies cannot leave it held."""

from __future__ import annotations

import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


class LockTimeout(Exception):
    pass


class FileLock:
    def __init__(self, path: Path, timeout: float = 60.0) -> None:
        self.path, self.timeout, self.fd = Path(path), timeout, None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.time() + self.timeout
        while True:
            try:
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                self.fd = fd
                return self
            except OSError:
                if time.time() >= deadline:
                    os.close(fd)
                    raise LockTimeout(f"{self.path} is held by another process")
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
