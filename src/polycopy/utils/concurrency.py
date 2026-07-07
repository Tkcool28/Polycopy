"""File-based concurrency guard using fcntl (Unix) or msvcrt (Windows).

Provides:
- FileLock: context manager for exclusive file locking
- LockError: raised when lock cannot be acquired within timeout
- lock_path(): helper to derive lock file path from a resource name

Usage:
    with FileLock("/tmp/polycopy_scan.lock", timeout=5.0):
        # exclusive access guaranteed
        run_scan()

This prevents multiple instances of run_scan.py or update_paper_portfolio.py
from running concurrently against the same database.
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class LockError(Exception):
    """Raised when a file lock cannot be acquired within the timeout."""

    def __init__(self, lock_path: str, timeout: float, pid: Optional[int] = None) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self.pid = pid
        msg = f"Could not acquire lock {lock_path} within {timeout}s"
        if pid:
            msg += f" (held by PID {pid})"
        super().__init__(msg)


# ── Process-wide lock tracking ─────────────────────────────────────────────────
# flock() on Linux is per-PID: the same process can re-acquire a lock it
# already holds without blocking, but opening a NEW fd for the same file
# and calling flock on it returns EACCES. We track held locks in a
# process-wide set so we can raise LockError immediately without
# opening/leaking file descriptors.
_held_locks: set[str] = set()


class FileLock:
    """Cross-platform file lock context manager.

    Uses fcntl on Unix and msvcrt on Windows. The lock is released when
    the context manager exits (even on exception).

    Attributes:
        lock_path: path to the lock file.
        timeout: max seconds to wait for lock acquisition.
        poll_interval: seconds between lock acquisition attempts.
        stale_after: seconds after which a lock is considered stale (orphaned).
    """

    def __init__(
        self,
        lock_path: str | Path,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
        stale_after: float = 3600.0,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self._fd: Optional[int] = None
        self._locked = False
        self._key: Optional[str] = None

    def __enter__(self) -> "FileLock":
        self._acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._release()

    def _acquire(self) -> None:
        """Acquire the exclusive lock, waiting up to self.timeout seconds."""
        start = time.monotonic()

        # Ensure parent directory exists
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve to absolute path for tracking
        key = str(self.lock_path.resolve())

        # If this process already holds this lock, raise immediately
        # (flock is per-PID so we can't acquire the same file twice)
        if key in _held_locks:
            raise LockError(
                str(self.lock_path),
                self.timeout,
                pid=os.getpid(),
            )

        while True:
            fd = None
            try:
                # Open (or create) the lock file
                fd = os.open(
                    str(self.lock_path),
                    os.O_RDWR | os.O_CREAT,
                    0o644,
                )
                self._try_lock(fd)
                # Write PID to lock file for stale detection
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.fsync(fd)
                self._fd = fd
                self._locked = True
                self._key = key
                _held_locks.add(key)
                logger.debug("Lock acquired: %s", self.lock_path)
                return

            except (OSError, IOError) as e:
                # Close the fd we just opened. Verified safe: closing a fd
                # whose flock() failed with EWOULDBLOCK does NOT block on
                # Linux (the kernel only blocks on flock()/fcntl() on
                # already-locked fds, not on close). Without this close
                # every retry leaks one fd and the process eventually hits
                # EMFILE under contention. PR24D made this visible.
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                fd = None

                # Determine if this is a lock-held error vs. a real failure
                import errno as _errno
                is_lock_held = False
                if isinstance(e, (OSError, IOError)):
                    if getattr(e, "errno", None) in (_errno.EACCES, _errno.EWOULDBLOCK, _errno.EAGAIN):
                        is_lock_held = True

                if not is_lock_held:
                    raise

                elapsed = time.monotonic() - start
                if elapsed >= self.timeout:
                    raise LockError(
                        str(self.lock_path),
                        self.timeout,
                        pid=None,
                    ) from e

                remaining = self.timeout - elapsed
                wait = min(self.poll_interval, remaining)
                if wait > 0:
                    time.sleep(wait)

    def _try_lock(self, fd: int) -> None:
        """Attempt to acquire the lock on the given fd. Raises OSError if already locked."""
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, max(1, 0x7FFFFFFF))
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EDEADLK):
                    raise OSError(errno.EACCES, "Lock held by another process") from e
                raise
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError) as e:
                if e.errno in (errno.EACCES, errno.EWOULDBLOCK):
                    raise OSError(errno.EACCES, "Lock held by another process") from e
                raise

    def _release(self) -> None:
        """Release the lock and close the file descriptor.

        Note: On Linux, close() on an fd with flock will implicitly
        release the lock. We skip the explicit LOCK_UN to avoid blocking.
        """
        if not self._locked or self._fd is None:
            return

        fd = self._fd
        self._fd = None
        self._locked = False

        # Remove from process-wide tracking
        if self._key is not None:
            _held_locks.discard(self._key)
            self._key = None

        try:
            os.close(fd)
            logger.debug("Lock released (close): %s", self.lock_path)
        except OSError as e:
            logger.warning("Error closing lock fd %s: %s", self.lock_path, e)

        # Remove lock file (best effort)
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_lock_pid(self) -> Optional[int]:
        """Read the PID from the lock file (for diagnostics).

        Uses low-level os.open/os.read to avoid blocking on a locked file.
        """
        try:
            fd = os.open(str(self.lock_path), os.O_RDONLY)
            try:
                data = os.read(fd, 32).decode().strip()
                return int(data) if data.isdigit() else None
            finally:
                os.close(fd)
        except (OSError, ValueError):
            return None

    def _is_stale(self) -> bool:
        """Check if the lock is stale (holder PID no longer exists)."""
        try:
            pid = self._read_lock_pid()
            if pid is None:
                return False
            # Check if process exists
            os.kill(pid, 0)
            return False
        except (OSError, ProcessLookupError):
            return True
        except (ValueError, AttributeError):
            return False

    def _break_stale_lock(self) -> None:
        """Remove a stale lock file and retry."""
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def lock_path(resource: str, base_dir: Optional[Path] = None) -> Path:
    """Derive a lock file path for a resource name.

    Args:
        resource: e.g. "scan", "portfolio_update", "settlement"
        base_dir: directory for lock files (default: /tmp)

    Returns:
        Path like /tmp/polycopy_scan.lock
    """
    if base_dir is None:
        base_dir = Path("/tmp")
    return base_dir / f"polycopy_{resource}.lock"
