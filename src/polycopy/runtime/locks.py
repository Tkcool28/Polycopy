"""Global no-overlap lock for Polycopy operational jobs (PR24D).

The four operational scripts (``collect_smart_money_data``,
``run_scan``, ``settle_paper_positions``, ``update_paper_portfolio``)
all write to the same SQLite database. SQLite serializes writes via
``busy_timeout``, but that just makes jobs pile up — there is no
guarantee they won't overlap and stomp on each other's transactions.

PR24D introduces a single shared lock path. Any operational job that
holds the lock blocks every other operational job from starting. The
lock has a short, explicit timeout (fail-fast). On acquisition failure
the caller must exit nonzero so timers / operators can see the conflict
instead of silently retrying.

Design notes:
- Reuses ``polycopy.utils.concurrency.FileLock`` (fcntl on Unix,
  msvcrt on Windows). Same proven primitive.
- One shared lock file across all four jobs. No per-script locks; that
  would create deadlock potential and defeats the no-overlap guarantee.
- Lock path defaults to ``/tmp/polycopy-operational-jobs.lock`` but can
  be overridden via the ``POLYCOPY_OPERATIONAL_LOCK_PATH`` env var.
- Logs at INFO: attempt, acquired, unavailable, released.
- Default timeout 30s. Each script's ``--lock-timeout`` overrides per-run.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from polycopy.utils.concurrency import FileLock, LockError

logger = logging.getLogger(__name__)


# Single shared lock path for ALL operational jobs.
DEFAULT_OPERATIONAL_LOCK_PATH = "/tmp/polycopy-operational-jobs.lock"
OPERATIONAL_LOCK_ENV_VAR = "POLYCOPY_OPERATIONAL_LOCK_PATH"

# Default timeout in seconds. Fail-fast: 30s is plenty for any normal
# operational job to finish its current work; if a job holds the lock
# longer than that, something is wrong and the new job should exit
# nonzero so timers / operators can see it.
DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S = 30.0

# Recognised job names. Used only for logging — not for the lock path.
# Each script should pass a short, recognisable name.
KNOWN_JOB_NAMES = frozenset({"collect", "scan", "settle", "update"})


def operational_lock_path() -> Path:
    """Resolve the shared lock file path.

    Priority:
    1. ``POLYCOPY_OPERATIONAL_LOCK_PATH`` env var (if set and non-empty).
    2. ``DEFAULT_OPERATIONAL_LOCK_PATH`` (/tmp/polycopy-operational-jobs.lock).
    """
    env = os.environ.get(OPERATIONAL_LOCK_ENV_VAR, "").strip()
    if env:
        return Path(env)
    return Path(DEFAULT_OPERATIONAL_LOCK_PATH)


@contextmanager
def operational_job_lock(
    job_name: str,
    *,
    timeout: float = DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
    lock_path: Optional[Path] = None,
) -> Iterator[FileLock]:
    """Context manager that acquires the global operational-jobs lock.

    Args:
        job_name: short name of the calling job (``"collect"``, ``"scan"``,
            ``"settle"``, ``"update"``). Used for log lines only.
        timeout: seconds to wait for lock acquisition before giving up.
            Default 30s. Pass ``0`` for fail-fast (raise immediately if
            another holder exists).
        lock_path: optional override for the lock file path. Defaults to
            :func:`operational_lock_path`.

    Yields:
        The acquired :class:`FileLock` instance (mostly for diagnostics).

    Raises:
        LockError: if the lock cannot be acquired within ``timeout`` seconds.
            The caller should catch this, log, and exit nonzero.

    The lock is always released — on normal exit, on exception, and on
    ``LockError`` raised by the underlying FileLock (the context manager
    ``__exit__`` runs even on exception).

    Example::

        with operational_job_lock("collect", timeout=args.lock_timeout):
            db = Database(db_path=settings.db_path).connect()
            try:
                run_collection(db=db)
            finally:
                db.close()
    """
    if not job_name or not job_name.strip():
        raise ValueError("job_name must be a non-empty string")

    path = lock_path if lock_path is not None else operational_lock_path()
    lock = FileLock(lock_path=path, timeout=timeout, poll_interval=0.25)

    logger.info(
        "Attempting to acquire operational lock for job=%s timeout=%.1fs path=%s",
        job_name,
        timeout,
        path,
    )
    try:
        with lock:
            logger.info(
                "Acquired operational lock for job=%s path=%s",
                job_name,
                path,
            )
            try:
                yield lock
            finally:
                # Logged after the body exits but before FileLock.__exit__
                # releases the OS-level lock. This gives operators a clear
                # "we're done with the lock" signal in the logs even if
                # the inner code raised.
                logger.info(
                    "Releasing operational lock for job=%s path=%s",
                    job_name,
                    path,
                )
    except LockError as e:
        logger.error(
            "Operational lock UNAVAILABLE for job=%s path=%s: %s "
            "(another operational job is likely still running; "
            "exit nonzero so timers / operators see the conflict)",
            job_name,
            path,
            e,
        )
        raise
    except Exception:
        # FileLock.__exit__ already released the lock; we just re-raise.
        logger.exception(
            "Operational lock released for job=%s path=%s after exception",
            job_name,
            path,
        )
        raise


__all__ = [
    "DEFAULT_OPERATIONAL_LOCK_PATH",
    "DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S",
    "KNOWN_JOB_NAMES",
    "OPERATIONAL_LOCK_ENV_VAR",
    "FileLock",
    "LockError",
    "operational_job_lock",
    "operational_lock_path",
]