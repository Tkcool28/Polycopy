"""Dedicated hardened advisory lock for bounded-observation control files."""
from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from typing import Self


class ControlLockError(RuntimeError):
    """Control lock cannot be acquired safely or within its timeout."""


def _validate_fd(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ControlLockError("control lock must be a regular file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ControlLockError("control lock owner does not match effective user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ControlLockError("control lock permissions must be 0600")


class ControlLock:
    """Linux-safe, non-symlink-following advisory lock; never unlinks the path."""

    def __init__(self, path: Path | str, *, timeout: float = 0.0, poll_interval: float = 0.05) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.fd: int | None = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def acquire(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        start = time.monotonic()
        while True:
            try:
                fd = os.open(self.path, flags, 0o600)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EISDIR}:
                    raise ControlLockError("unsafe control lock path") from exc
                raise ControlLockError(f"cannot open control lock: {exc}") from exc
            try:
                _validate_fd(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                        raise ControlLockError(f"cannot acquire control lock: {exc}") from exc
                    if time.monotonic() - start >= self.timeout:
                        raise ControlLockError("control lock acquisition timed out") from exc
                    os.close(fd)
                    time.sleep(self.poll_interval)
                    continue
                # PID diagnostics are written only after secure advisory acquisition.
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.fsync(fd)
                self.fd = fd
                return
            except Exception:
                os.close(fd)
                raise

    def release(self) -> None:
        if self.fd is None:
            return
        import fcntl

        fd, self.fd = self.fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
