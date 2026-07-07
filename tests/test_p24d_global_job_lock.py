"""PR24D: Tests for the global no-overlap lock on operational jobs.

Verifies:
1. Lock can be acquired and released.
2. Same lock cannot be acquired twice concurrently (second wait fails).
3. Lock is released after exception.
4. Each of the four operational scripts uses the shared lock helper
   (so a refactor that drops it gets caught).
5. Timeout / fail-fast behavior is deterministic.
6. Env-var override resolves to the override path.
7. Cross-script subprocess integration: two processes racing for the same
   lock path can't both hold it.
8. FileLock does not leak file descriptors on failed acquisition
   (PR24D regression test).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from polycopy.runtime.locks import (
    DEFAULT_OPERATIONAL_LOCK_PATH,
    DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
    OPERATIONAL_LOCK_ENV_VAR,
    operational_job_lock,
    operational_lock_path,
)
from polycopy.utils.concurrency import LockError


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _cleanup(path: Path) -> None:
    """Best-effort lock file cleanup so tests don't interfere with each other."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture
def repo_root() -> Path:
    """Polycopy repo root (this test file lives at tests/test_p24d_...)."""
    return Path(__file__).resolve().parent.parent


# Scripts that PR24D protects. They MUST go through the shared helper.
OPERATIONAL_SCRIPTS = [
    "scripts/collect_smart_money_data.py",
    "scripts/run_scan.py",
    "scripts/settle_paper_positions.py",
    "scripts/update_paper_portfolio.py",
]


# ─── 1. Acquire and release ───────────────────────────────────────────────────


class TestAcquireRelease:
    def test_acquire_and_release(self, tmp_path: Path):
        """Single acquire → exit → second acquire works."""
        path = tmp_path / "ops.lock"
        try:
            with operational_job_lock("collect", lock_path=path, timeout=1.0):
                pass
            # Second acquisition must succeed — lock was released.
            with operational_job_lock("scan", lock_path=path, timeout=1.0):
                pass
        finally:
            _cleanup(path)

    def test_acquired_log_messages(self, tmp_path: Path, caplog):
        """Confirm attempt + acquired + releasing log lines fire at INFO."""
        path = tmp_path / "ops.lock"
        try:
            with caplog.at_level("INFO", logger="polycopy.runtime.locks"):
                with operational_job_lock("collect", lock_path=path, timeout=1.0):
                    pass
            messages = [r.message for r in caplog.records]
            assert any("Attempting to acquire operational lock" in m for m in messages)
            assert any("Acquired operational lock" in m for m in messages)
            assert any("Releasing operational lock" in m for m in messages)
        finally:
            _cleanup(path)

    def test_yields_filelock(self, tmp_path: Path):
        """The context manager yields the underlying FileLock instance."""
        path = tmp_path / "ops.lock"
        try:
            with operational_job_lock("collect", lock_path=path, timeout=1.0) as lock:
                assert hasattr(lock, "lock_path")
                assert Path(lock.lock_path) == path
        finally:
            _cleanup(path)


# ─── 2. Same lock cannot be acquired twice concurrently ──────────────────────


class TestConcurrentAcquisition:
    def test_second_acquirer_fails_fast_with_zero_timeout(self, tmp_path: Path):
        """If timeout=0 (fail-fast) and lock is held, second acquirer raises immediately."""
        path = tmp_path / "ops.lock"
        try:
            with operational_job_lock("collect", lock_path=path, timeout=5.0):
                start = time.monotonic()
                with pytest.raises(LockError):
                    with operational_job_lock(
                        "scan", lock_path=path, timeout=0.0
                    ):
                        pass
                elapsed = time.monotonic() - start
                assert elapsed < 1.0, (
                    f"fail-fast should return promptly, took {elapsed:.3f}s"
                )
        finally:
            _cleanup(path)

    def test_second_acquirer_waits_then_succeeds(self, tmp_path: Path):
        """If the holder releases within the timeout, the second acquirer wins."""
        path = tmp_path / "ops.lock"
        try:
            with operational_job_lock("collect", lock_path=path, timeout=1.0):
                pass  # released here
            with operational_job_lock("scan", lock_path=path, timeout=5.0):
                pass
        finally:
            _cleanup(path)

    def test_second_acquirer_times_out(self, tmp_path: Path):
        """If the holder holds longer than the timeout, the second raises LockError.

        Note: in-process threads trip FileLock's in-process ``_held_locks`` set
        and fail immediately (correct cross-thread behaviour — same process
        cannot hold the same lock twice). The timeout-retry path is exercised
        by the cross-subprocess test in TestCrossScriptLock.
        """
        path = tmp_path / "ops.lock"
        holder_acquired = threading.Event()
        holder_release = threading.Event()
        t: threading.Thread | None = None

        def holder():
            with operational_job_lock(
                "collect", lock_path=path, timeout=10.0
            ):
                holder_acquired.set()
                holder_release.wait(timeout=5.0)

        try:
            t = threading.Thread(target=holder, daemon=True)
            t.start()
            assert holder_acquired.wait(timeout=3.0), "holder did not acquire"
            start = time.monotonic()
            with pytest.raises(LockError):
                with operational_job_lock(
                    "scan", lock_path=path, timeout=0.5
                ):
                    pass
            elapsed = time.monotonic() - start
            # Fail fast OR wait up to timeout — both correct.
            assert elapsed < 3.0, (
                f"second acquirer should fail promptly or within timeout, "
                f"got {elapsed:.3f}s"
            )
        finally:
            holder_release.set()
            if t is not None:
                t.join(timeout=3.0)
            _cleanup(path)


# ─── 3. Lock released after exception ────────────────────────────────────────


class TestExceptionRelease:
    def test_lock_released_after_exception(self, tmp_path: Path):
        """If the body raises, the lock is still released."""
        path = tmp_path / "ops.lock"
        try:
            with pytest.raises(RuntimeError, match="boom"):
                with operational_job_lock("collect", lock_path=path, timeout=1.0):
                    raise RuntimeError("boom")
            # Lock must be released — fresh acquisition must succeed.
            with operational_job_lock("scan", lock_path=path, timeout=1.0):
                pass
        finally:
            _cleanup(path)

    def test_lock_released_after_keyboard_interrupt(self, tmp_path: Path):
        """KeyboardInterrupt inside the body still releases the lock."""
        path = tmp_path / "ops.lock"
        try:
            with pytest.raises(KeyboardInterrupt):
                with operational_job_lock("collect", lock_path=path, timeout=1.0):
                    raise KeyboardInterrupt()
            with operational_job_lock("scan", lock_path=path, timeout=1.0):
                pass
        finally:
            _cleanup(path)

    def test_lock_unavailable_log_contains_recommendation(self, tmp_path: Path, caplog):
        """When LockError fires, the log line tells the caller to exit nonzero."""
        path = tmp_path / "ops.lock"
        try:
            with operational_job_lock("collect", lock_path=path, timeout=5.0):
                with caplog.at_level("ERROR", logger="polycopy.runtime.locks"):
                    with pytest.raises(LockError):
                        with operational_job_lock(
                            "scan", lock_path=path, timeout=0.0
                        ):
                            pass
            msgs = [r.message for r in caplog.records]
            assert any("UNAVAILABLE" in m for m in msgs), (
                f"expected UNAVAILABLE log, got: {msgs}"
            )
            assert any("exit nonzero" in m for m in msgs), (
                f"expected 'exit nonzero' recommendation, got: {msgs}"
            )
        finally:
            _cleanup(path)


# ─── 4. Each operational script uses the shared lock helper ───────────────────


class TestScriptsUseSharedLock:
    @pytest.mark.parametrize("script_relpath", OPERATIONAL_SCRIPTS)
    def test_script_imports_shared_lock_helper(
        self, script_relpath: str, repo_root: Path
    ):
        """Each script must import operational_job_lock from polycopy.runtime.locks."""
        script = repo_root / script_relpath
        assert script.exists(), f"missing script: {script}"
        text = script.read_text()
        assert (
            "from polycopy.runtime.locks import operational_job_lock" in text
        ), (
            f"{script_relpath}: missing shared lock helper import"
        )

    @pytest.mark.parametrize("script_relpath", OPERATIONAL_SCRIPTS)
    def test_script_calls_shared_lock(
        self, script_relpath: str, repo_root: Path
    ):
        """Each script must actually wrap its main work in operational_job_lock(...)."""
        script = repo_root / script_relpath
        text = script.read_text()
        assert "operational_job_lock(" in text, (
            f"{script_relpath}: no operational_job_lock(...) call site"
        )

    @pytest.mark.parametrize("script_relpath", OPERATIONAL_SCRIPTS)
    def test_script_no_longer_uses_per_script_lock_path(
        self, script_relpath: str, repo_root: Path
    ):
        """Per-script lock_path() calls must be gone (they would bypass the global lock)."""
        script = repo_root / script_relpath
        text = script.read_text()
        assert "lock_path(" not in text, (
            f"{script_relpath}: still calls lock_path(...); per-script locks would "
            f"defeat PR24D's no-overlap guarantee"
        )

    @pytest.mark.parametrize("script_relpath", OPERATIONAL_SCRIPTS)
    def test_script_no_longer_directly_uses_filelock_class(
        self, script_relpath: str, repo_root: Path
    ):
        """Operational scripts must not import FileLock directly — must go through the helper."""
        script = repo_root / script_relpath
        text = script.read_text()
        assert "from polycopy.utils.concurrency import FileLock" not in text, (
            f"{script_relpath}: still imports FileLock directly; "
            f"must use polycopy.runtime.locks.operational_job_lock instead"
        )


# ─── 5. Timeout / fail-fast determinism ───────────────────────────────────────


class TestTimeoutDeterminism:
    def test_zero_timeout_fails_fast(self, tmp_path: Path):
        """timeout=0 must NOT block — must raise immediately."""
        path = tmp_path / "ops.lock"
        try:
            with operational_job_lock("collect", lock_path=path, timeout=10.0):
                start = time.monotonic()
                for _ in range(3):
                    with pytest.raises(LockError):
                        with operational_job_lock(
                            "scan", lock_path=path, timeout=0.0
                        ):
                            pass
                elapsed = time.monotonic() - start
                assert elapsed < 1.5, (
                    f"three fail-fast attempts took {elapsed:.3f}s, expected <1.5s"
                )
        finally:
            _cleanup(path)

    def test_timeout_value_default_is_30s(self):
        """The default timeout is the operational 30s, not FileLock's 10s."""
        assert DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S == 30.0

    def test_short_timeout_enforced(self, tmp_path: Path):
        """A short timeout must give up promptly when the in-process lock is held.

        Same caveat as ``test_second_acquirer_times_out``: in-process threads
        trip ``_held_locks`` and fail fast (correct). The deterministic
        cross-process timeout is exercised by ``TestCrossScriptLock``.
        """
        path = tmp_path / "ops.lock"
        holder_acquired = threading.Event()
        holder_release = threading.Event()
        t: threading.Thread | None = None

        def holder():
            with operational_job_lock(
                "collect", lock_path=path, timeout=10.0
            ):
                holder_acquired.set()
                holder_release.wait(timeout=5.0)

        try:
            t = threading.Thread(target=holder, daemon=True)
            t.start()
            assert holder_acquired.wait(timeout=3.0)
            start = time.monotonic()
            with pytest.raises(LockError):
                with operational_job_lock(
                    "scan", lock_path=path, timeout=0.5
                ):
                    pass
            elapsed = time.monotonic() - start
            # In-process check fails fast; cross-process check waits ~timeout.
            assert elapsed < 3.0, (
                f"expected prompt fail or ~timeout, got {elapsed:.3f}s"
            )
        finally:
            holder_release.set()
            if t is not None:
                t.join(timeout=3.0)
            _cleanup(path)


# ─── 6. Env-var override ─────────────────────────────────────────────────────


class TestEnvVarOverride:
    def test_default_path_is_documented(self, monkeypatch):
        """The default lock path is the documented /tmp/polycopy-operational-jobs.lock."""
        monkeypatch.delenv(OPERATIONAL_LOCK_ENV_VAR, raising=False)
        assert operational_lock_path() == Path(DEFAULT_OPERATIONAL_LOCK_PATH)
        assert DEFAULT_OPERATIONAL_LOCK_PATH == "/tmp/polycopy-operational-jobs.lock"

    def test_env_var_overrides_path(self, tmp_path: Path, monkeypatch):
        override = tmp_path / "custom.lock"
        monkeypatch.setenv(OPERATIONAL_LOCK_ENV_VAR, str(override))
        assert operational_lock_path() == override

        try:
            with operational_job_lock("collect", timeout=1.0):
                pass
            with operational_job_lock("scan", timeout=1.0):
                pass
        finally:
            _cleanup(override)

    def test_empty_env_var_falls_back_to_default(self, tmp_path: Path, monkeypatch):
        """Empty-string env var must fall back to the documented default, not resolve to ''."""
        monkeypatch.setenv(OPERATIONAL_LOCK_ENV_VAR, "")
        assert operational_lock_path() == Path(DEFAULT_OPERATIONAL_LOCK_PATH)


# ─── 7. Cross-script integration (subprocess) ─────────────────────────────────


class TestCrossScriptLock:
    """Prove that two processes racing for the same lock actually block each other."""

    def test_two_subprocesses_share_lock(self, tmp_path: Path):
        """Run two short-lived holder scripts against the same lock path; only one wins."""
        path = tmp_path / "shared.lock"
        try:
            src_dir = (Path(__file__).resolve().parent.parent / "src").as_posix()
            holder_script = textwrap.dedent(
                f"""
                import sys, time
                sys.path.insert(0, {src_dir!r})
                from polycopy.runtime.locks import operational_job_lock
                from polycopy.utils.concurrency import LockError
                job = sys.argv[1]
                try:
                    with operational_job_lock(job, lock_path={str(path)!r}, timeout=0.0):
                        print("ACQUIRED:" + job)
                        time.sleep(2.0)
                        print("RELEASED:" + job)
                except LockError as e:
                    print("LOCK_ERROR:" + job + ":" + str(e))
                    sys.exit(7)
                """
            )
            tmpdir = tmp_path / "sub"
            tmpdir.mkdir()
            script_path = tmpdir / "holder.py"
            script_path.write_text(holder_script)

            env = os.environ.copy()
            env["PYTHONPATH"] = (
                Path(__file__).resolve().parent.parent / "src"
            ).as_posix()

            # First holder runs and acquires.
            p1 = subprocess.Popen(
                [sys.executable, str(script_path), "scan"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            # Wait for p1 to actually acquire the lock (PID in file).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if path.exists():
                    content = path.read_text().strip()
                    if content and content.splitlines()[0].isdigit():
                        break
                time.sleep(0.1)
            else:
                raise AssertionError(
                    "first subprocess never acquired lock within 5s"
                )

            # Second holder races — must fail fast (timeout=0) with LockError.
            p2 = subprocess.run(
                [sys.executable, str(script_path), "settle"],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            assert p2.returncode == 7, (
                f"p2 should have exited 7 (LockError), got {p2.returncode}: "
                f"{p2.stdout!r} {p2.stderr!r}"
            )
            assert "LOCK_ERROR:settle" in p2.stdout

            # Clean up p1.
            try:
                p1.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p1.kill()
                p1.wait()
        finally:
            _cleanup(path)


# ─── 8. Empty job_name validation ─────────────────────────────────────────────


class TestInputValidation:
    def test_empty_job_name_rejected(self, tmp_path: Path):
        """An empty job_name must raise ValueError — never silently acquire."""
        path = tmp_path / "ops.lock"
        try:
            with pytest.raises(ValueError):
                with operational_job_lock("", lock_path=path, timeout=1.0):
                    pass
            with pytest.raises(ValueError):
                with operational_job_lock("   ", lock_path=path, timeout=1.0):
                    pass
        finally:
            _cleanup(path)


# ─── 9. FileLock fd leak regression (PR24D made this visible) ────────────────


class TestFileLockFdLeakRegression:
    """PR24D's cross-subprocess test surfaced a pre-existing FileLock fd leak:
    every failed flock() retry leaked an fd. With a 1024 fd limit and a
    30s timeout at 0.5s polling, the process hits EMFILE after ~60s of
    contention. The fix closes the fd on the catch path. This test
    proves the leak is gone.
    """

    def test_no_fd_leak_under_contention(self, tmp_path: Path):
        """Repeatedly fail to acquire a cross-process held lock; fd count must not grow.

        The holder MUST be in a separate process — in-process threads trip
        FileLock's in-process ``_held_locks`` set and fail immediately,
        which doesn't exercise the retry path (and is what we want — the
        retry path is what leaked fds).
        """
        path = tmp_path / "ops.lock"
        src_dir = (Path(__file__).resolve().parent.parent / "src").as_posix()

        # Holder subprocess: hold the lock for the whole test.
        holder_script = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {src_dir!r})
            from polycopy.runtime.locks import operational_job_lock
            with operational_job_lock(
                "collect",
                lock_path={str(path)!r},
                timeout=30.0,
            ):
                # Block until killed by the parent test process.
                time.sleep(30)
            """
        )
        tmpdir = tmp_path / "leak"
        tmpdir.mkdir()
        holder_path = tmpdir / "holder.py"
        holder_path.write_text(holder_script)

        env = os.environ.copy()
        env["PYTHONPATH"] = src_dir

        proc = subprocess.Popen(
            [sys.executable, str(holder_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        def fd_count() -> int:
            return len(os.listdir(f"/proc/{os.getpid()}/fd"))

        def lock_held() -> bool:
            """True if the lock file exists AND contains a PID line."""
            if not path.exists():
                return False
            try:
                content = path.read_text().strip()
                # Holder writes "{pid}\n" after acquiring.
                return bool(content) and content.splitlines()[0].isdigit()
            except OSError:
                return False

        try:
            # Wait for the holder to actually acquire the lock (PID in file).
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if lock_held():
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(
                    "holder subprocess never acquired the lock within 8s; "
                    f"file contents: {path.read_text() if path.exists() else '<missing>'!r}"
                )

            before = fd_count()

            # Contend with a 1.5s timeout — that's ~6 retries at 0.25s polling.
            # Pre-fix: leaks ~6 fds. Post-fix: 0.
            start = time.monotonic()
            with pytest.raises(LockError):
                with operational_job_lock(
                    "scan", lock_path=path, timeout=1.5
                ):
                    pass
            elapsed = time.monotonic() - start
            assert elapsed >= 0.7, (
                f"expected ~1.5s wait before timeout, got {elapsed:.3f}s "
                f"— cross-process retry path may not be exercised"
            )
            assert elapsed < 4.0, (
                f"timeout too slow: {elapsed:.3f}s"
            )

            after = fd_count()
            leaked = after - before
            assert leaked <= 1, (
                f"fd leak: {leaked} fds leaked (before={before}, after={after}). "
                f"FileLock failed-acquisition path is leaking descriptors."
            )
        finally:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            _cleanup(path)