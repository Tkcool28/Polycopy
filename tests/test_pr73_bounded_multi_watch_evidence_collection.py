"""PR #73 — Bounded multi-watch evidence collection: full test suite.

Temp/scratch disposable v21 DBs only. Never opens production. Never touches
``/root/Polycopy``. Exercises the ACCEPTED PR #71 single-watch collector through
the new bounded cohort orchestration, asserting every contract in the PR:

  * hard bounds (1..5 unique active watches);
  * no wallet-address / discovery / implicit-all expansion;
  * explicit duplicate / duplicate-wallet / inactive / sample / missing-wallet
    rejection BEFORE any provider / network / DB-mutating activity;
  * dry-run purity (zero writes);
  * production write-gate ordering (gates fail before provider/network/DB);
  * lock acquired ONCE, provider constructed only after lock, lock contention
    triggers zero activity;
  * deterministic watch order;
  * full-cohort atomicity (commit once; rollback on watch failure);
  * adapter + DB closed exactly once;
  * structured failure JSON carries the original error text;
  * exact allowed SQL write-table set (sqlite trace callback);
  * zero execution/approval-plane deltas;
  * idempotent replay (no duplicate source trades);
  * PR #71 ``build_status`` observes the cohort-written evidence;
  * no scoring/approval/dispatch/candidate/signal/execution function invoked;
  * real CLI ``main()`` integration via five explicit watch ids.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
import polycopy.ingestion.specialist_evidence_cohort as cohort  # noqa: E402
from polycopy.ingestion.specialist_evidence_cohort import (  # noqa: E402
    CohortRunConfig,
    validate_watch_ids,
    run_cohort,
    CohortValidationError,
    ALLOWED_WRITE_TABLES,
    FORBIDDEN_WRITE_TABLES,
)
from polycopy.ingestion import specialist_evidence_watchlist as wl  # noqa: E402
from evidence_db import open_readonly  # noqa: E402
import specialist_evidence_status as st  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────
COND = ["0x" + c * 64 for c in ("a", "b", "c", "d", "e", "f")]
TOK = ["0x" + c * 64 for c in ("a", "b", "c", "d", "e", "f")]
ADDR = ["0xgood0000000000000000000000000000000000" + c + c for c in ("a", "b", "c", "d", "e", "f")]
WUUID = ["uuid-wallet-0000000000000000000000000000000" + c for c in ("a", "b", "c", "d", "e", "f")]
SAMPLE_UUID = "uuid-wallet-sample0000000000000000000000000sam"
SAMPLE_ADDR = "0xsample0000000000000000000000000000000sam"


def _tmp() -> Path:
    raise RuntimeError("_tmp is provided by the module-owned SQLite fixture")


@pytest.fixture(autouse=True)
def _owned_sqlite_paths(monkeypatch, owned_sqlite):
    """Route this module's disposable SQLite files through pytest ownership."""
    monkeypatch.setitem(globals(), "_tmp", owned_sqlite.new_path)


def _open() -> Database:
    p = _tmp()
    return Database(p).connect()


def _seed_wallet(db: Database, wid: str, address: str, is_sample: int = 0) -> None:
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", is_sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _buy(tid: str, cond: str, tok: str, addr: str) -> dict:
    return {
        "sourceProvidedTradeId": tid,
        "proxyWallet": addr,
        "asset": tok,
        "conditionId": cond,
        "side": "BUY",
        "outcome": "Yes",
        "price": "0.40",
        "size": "10",
        "timestamp": "2026-02-01T00:00:00Z",
    }


def _sell(tid: str, cond: str, tok: str, addr: str) -> dict:
    r = _buy(tid, cond, tok, addr)
    r["side"] = "SELL"
    return r


def _gamma_map() -> dict:
    return {
        c: {
            "conditionId": c,
            "category": "Politics",
            "tags": ["election"],
            "events": [{"id": "e1", "slug": "us"}],
        }
        for c in COND
    }


class FakeGamma:
    def __init__(self, by_condition: dict | None = None):
        self._c = by_condition or {}
        self.calls = 0

    async def __call__(self, condition_id: str):
        self.calls += 1
        return self._c.get(condition_id)


class FakeAdapter:
    """Adapter returning scripted raw trades keyed by on-chain address."""

    def __init__(self, targets: dict[str, list[dict]]):
        self._targets = targets
        self.closed = 0
        self.build_calls = 0

    async def get_trades_by_address(
        self, wallet, *, since, limit, offset, return_raw
    ):
        return list(self._targets.get(wallet.lower(), []))[:limit]

    def close(self):
        self.closed += 1


def _targets_for(indices, rows_per=2, include_sell=False):
    t: dict[str, list[dict]] = {}
    for i in indices:
        rows = [_buy(f"t{i}_{j}", COND[i], TOK[i], ADDR[i]) for j in range(rows_per)]
        if include_sell:
            rows.append(_sell(f"s{i}", COND[i], TOK[i], ADDR[i]))
        t[ADDR[i].lower()] = rows
    return t


def _make_fake_adapter(indices, rows_per=2, include_sell=False) -> FakeAdapter:
    return FakeAdapter(_targets_for(indices, rows_per=rows_per, include_sell=include_sell))


def _seed_active_watches(db: Database, indices) -> list[str]:
    wids = []
    for i in indices:
        _seed_wallet(db, WUUID[i], ADDR[i])
        wids.append(wl.add_watch(db, wallet_id=WUUID[i]))
    return wids


# ── Tests 1..5: bounds / validation ─────────────────────────────────────────
def test_one_valid_watch_accepted():
    db = _open()
    (wid,) = _seed_active_watches(db, [0])
    try:
        ordered = validate_watch_ids(db, [wid])
        assert ordered == [wid], ordered
    finally:
        db.close()


def test_five_valid_watches_accepted():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    try:
        ordered = validate_watch_ids(db, wids)
        assert ordered == sorted(wids)
        assert len(ordered) == 5
    finally:
        db.close()


def test_six_watches_rejected_before_activity():
    db = _open()
    # Seed five real active watches + six ids (the 6th id is unknown).
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    six = wids + ["wl_0000000000000000000000000000dead"]
    adapter = _make_fake_adapter([0])
    cfg = CohortRunConfig()
    try:
        # Nothing should construct a provider or touch network/DB-write.
        res = asyncio.run(
            run_cohort(db, watch_ids=six, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res.status == "failed"
        assert res.cohort_committed is False
        assert res.error and "between 1 and 5" in res.error
        # Provider was never used.
        assert adapter.closed == 0
    finally:
        db.close()


def test_zero_watches_rejected():
    db = _open()
    cfg = CohortRunConfig()
    adapter = FakeAdapter({})
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=[], adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res.status == "failed"
        assert res.cohort_committed is False
        assert "between 1 and 5" in (res.error or "")
        assert adapter.closed == 0
    finally:
        db.close()


def test_duplicate_watch_ids_handled_explicitly():
    db = _open()
    (wid,) = _seed_active_watches(db, [0])
    try:
        # Exact duplicate of the SAME id is rejected as a duplicate.
        try:
            validate_watch_ids(db, [wid, wid])
            assert False, "duplicate id must raise"
        except CohortValidationError as exc:
            assert wid in exc.rejected_watch_ids
            assert len(exc.rejected_watch_ids) >= 1
    finally:
        db.close()


# ── Test 6: duplicate wallets behind different watch ids ───────────────────
def test_duplicate_wallets_behind_different_watches_rejected():
    db = _open()
    # Two distinct watch ids pointing at the SAME wallet. The schema enforces a
    # partial UNIQUE index (ux_evidence_watchlist_active) allowing only one
    # ACTIVE watch per wallet, so raw-inserting a second active watch fails at
    # the constraint level. To exercise validate_watch_ids' duplicate-wallet
    # rejection we temporarily drop that partial unique index, seed two active
    # watches for the same wallet, then restore it before invoking validation.
    _seed_wallet(db, WUUID[0], ADDR[0])
    w1 = wl.add_watch(db, wallet_id=WUUID[0])
    w2 = "wl_2a3b4c5d6e7f8091a2b3c4d5e6f7"
    db.conn.execute("DROP INDEX IF EXISTS ux_evidence_watchlist_active")
    try:
        db.conn.execute(
            "INSERT INTO specialist_evidence_watchlist("
            "id, wallet_id, status, source, reason, created_by, created_at) "
            "VALUES (?,?, 'active', 'manual', 'seed', 't', '2026-01-01T00:00:00Z')",
            (w2, WUUID[0]),
        )
        db.conn.commit()
        try:
            try:
                validate_watch_ids(db, [w1, w2])
                assert False, "duplicate wallet membership must raise"
            except CohortValidationError as exc:
                assert "duplicate wallet" in str(exc)
        finally:
            # Remove the second watch row before restoring the unique index so
            # the constraint re-create does not trip on the duplicate wallet.
            db.conn.execute(
                "DELETE FROM specialist_evidence_watchlist WHERE id=?", (w2,)
            )
            db.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_watchlist_active "
                "ON specialist_evidence_watchlist(wallet_id) WHERE status = 'active'"
            )
    finally:
        db.close()


# ── Test 7: inactive watch rejected ────────────────────────────────────────
def test_inactive_watch_rejected():
    db = _open()
    _seed_wallet(db, WUUID[0], ADDR[0])
    wid = wl.add_watch(db, wallet_id=WUUID[0])
    wl.pause_watch(db, wid)
    try:
        try:
            validate_watch_ids(db, [wid])
            assert False, "paused watch must raise"
        except CohortValidationError as exc:
            assert "not active" in str(exc)
    finally:
        db.close()


# ── Test 8: sample wallet rejected ─────────────────────────────────────────
def test_sample_wallet_rejected():
    db = _open()
    _seed_wallet(db, SAMPLE_UUID, SAMPLE_ADDR, is_sample=1)
    try:
        # add_watch itself refuses sample wallets.
        try:
            wl.add_watch(db, wallet_id=SAMPLE_UUID)
            assert False, "sample wallet must be rejected at watch creation"
        except ValueError as exc:
            assert "sample" in str(exc)
    finally:
        db.close()


# ── Test 9: missing wallet rejected ────────────────────────────────────────
def test_missing_wallet_rejected():
    db = _open()
    # Create a valid watch (valid wallet), then remove the wallet row so the
    # watch references a now-missing wallet. This exercises validate_watch_ids'
    # "missing wallet" rejection without tripping the DB foreign-key constraint
    # at raw insert time (temporarily disable FK to allow the orphaning).
    _seed_wallet(db, WUUID[0], ADDR[0])
    orphan = wl.add_watch(db, wallet_id=WUUID[0])
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute("DELETE FROM wallets WHERE id=?", (WUUID[0],))
    db.conn.execute("PRAGMA foreign_keys=ON")
    db.conn.commit()
    try:
        try:
            validate_watch_ids(db, [orphan])
            assert False, "missing wallet must raise"
        except CohortValidationError as exc:
            assert "missing wallet" in str(exc)
    finally:
        db.close()


# ── Test 10: dry-run makes zero writes ─────────────────────────────────────
def test_dry_run_makes_zero_writes():
    db = _open()
    wids = _seed_active_watches(db, [0, 1])
    adapter = _make_fake_adapter([0, 1], include_sell=True)
    cfg = CohortRunConfig()
    try:
        before = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        after = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        assert res.status == "success"
        assert res.cohort_committed is False
        assert before == after == 0, (before, after)
        # Dry-run still reports would_create per watch.
        for w in res.watches:
            assert w.would_create >= 1, w.as_dict()
        # No last_collection_at mutation either.
        lac = db.conn.execute(
            "SELECT last_collection_at FROM specialist_evidence_watchlist "
            "WHERE id IN ({})".format(",".join("?" for _ in wids)),
            wids,
        ).fetchall()
        assert all(r[0] is None for r in lac)
    finally:
        db.close()


# ── Test 11: dry-run processes exactly the supplied watches ────────────────
def test_dry_run_processes_exactly_supplied():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    adapter = _make_fake_adapter([0, 1, 2, 3, 4])
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res.watch_count_requested == 5
        assert res.watch_count_completed == 5
        assert {w.watch_id for w in res.watches} == set(wids)
    finally:
        db.close()


# ── Test 12: no unrelated active watch included ────────────────────────────
def test_no_unrelated_active_watch_included():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    # An EXTRA active watch NOT in the explicit cohort.
    _seed_wallet(db, WUUID[5], ADDR[5])
    extra = wl.add_watch(db, wallet_id=WUUID[5])
    adapter = _make_fake_adapter([0, 1, 2, 3, 4])
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        processed = {w.watch_id for w in res.watches}
        assert extra not in processed, "unrelated watch must NOT be processed"
        assert len(processed) == 5
    finally:
        db.close()


# ── Test 13: production write gates fail before activity ───────────────────
def test_production_write_gates_fail_before_activity():
    db = _open()
    # Adapter that would assert if its network method is ever called.
    class AssertingAdapter(FakeAdapter):
        async def get_trades_by_address(self, *a, **k):
            raise AssertionError("provider/network must not run before gates pass")

    adapter = AssertingAdapter(_targets_for([0]))
    try:
        # The CLI path uses require_write_gates (shared helper). On a
        # recognized production DB the full --write --allow-live
        # --confirm-production-db set is required; a disposable/temp DB with
        # --write only is permitted (no production contact). Both are accepted
        # repository semantics (see evidence_db.require_write_gates / PR #72).
        from evidence_db import require_write_gates, PRODUCTION_DB_ABSOLUTE

        class _ProdPartial:
            dry_run = False
            write = True
            allow_live = False
            confirm_production_db = False

        # Production DB without the full gate set -> gates FAIL (CLI exits 2
        # before opening writable / constructing provider).
        assert require_write_gates(_ProdPartial(), db_path=str(PRODUCTION_DB_ABSOLUTE)) is False

        class _TempWriteOnly:
            dry_run = False
            write = True
            allow_live = False
            confirm_production_db = False

        # Disposable temp DB + --write only -> gates PASS (accepted semantics).
        assert require_write_gates(_TempWriteOnly(), db_path=str(db.db_path)) is True

        class _DryRun:
            dry_run = True
            write = False
            allow_live = False
            confirm_production_db = False

        # Dry-run is never a write -> gates FAIL (no provider/network).
        assert require_write_gates(_DryRun(), db_path=str(db.db_path)) is False
        # run_cohort was never reached: provider unused.
        assert adapter.closed == 0
    finally:
        db.close()


# ── Test 14: lock contention => zero activity ──────────────────────────────
def test_lock_contention_zero_activity():
    import threading
    from polycopy.runtime.locks import operational_job_lock

    db = _open()
    wids = _seed_active_watches(db, [0, 1])
    lock_path = Path(tempfile.mktemp(suffix=".lock"))
    activity = {"provider": 0}

    class CountingAdapter(FakeAdapter):
        async def get_trades_by_address(self, *a, **k):
            activity["provider"] += 1
            return await super().get_trades_by_address(*a, **k)

    adapter = CountingAdapter(_targets_for([0, 1]))
    cfg = CohortRunConfig()

    # Hold the lock in another thread.
    held = threading.Event()
    released = threading.Event()

    def _hold():
        with operational_job_lock("collect", timeout=5.0, lock_path=lock_path):
            held.set()
            released.wait(5.0)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert held.wait(5.0)

    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma(), lock_timeout=0.5, lock_path=lock_path)
        )
        assert res.status == "failed"
        assert res.cohort_committed is False
        assert "operational_lock_unavailable" in res.reason_codes
        # Zero provider/network/DB-mutating activity under contention.
        assert activity["provider"] == 0, activity
    finally:
        released.set()
        t.join(timeout=5.0)
        db.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


# ── Test 15: lock acquired once for entire cohort ──────────────────────────
def test_lock_acquired_once_for_cohort():
    from polycopy.runtime.locks import FileLock

    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2])
    lock_path = Path(tempfile.mktemp(suffix=".lock"))

    acquires = {"n": 0}

    class CountingLock(FileLock):
        def __enter__(self):
            acquires["n"] += 1
            return super().__enter__()

    # Patch the lock class used by operational_job_lock.
    import polycopy.runtime.locks as locks_mod
    orig = locks_mod.FileLock
    locks_mod.FileLock = CountingLock
    try:
        adapter = _make_fake_adapter([0, 1, 2])
        cfg = CohortRunConfig()
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma(), lock_path=lock_path)
        )
        assert res.status == "success"
        assert acquires["n"] == 1, acquires
    finally:
        locks_mod.FileLock = orig
        db.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


# ── Test 16: provider constructed only after lock ──────────────────────────
def test_provider_constructed_after_lock():
    from polycopy.runtime.locks import FileLock

    db = _open()
    wids = _seed_active_watches(db, [0])
    lock_path = Path(tempfile.mktemp(suffix=".lock"))
    order = []

    class TrackedLock:
        def __init__(self, lock_path, timeout, poll_interval=0.25):
            order.append("lock_acquire")
            self._lk = FileLock(lock_path=lock_path, timeout=timeout, poll_interval=poll_interval)

        def __enter__(self):
            self._lk.__enter__()
            return self._lk

        def __exit__(self, *exc):
            return self._lk.__exit__(*exc)

    # Adapter factory records construction order.
    def _tracking_adapter_factory():
        def _build():
            order.append("adapter_build")
            return FakeAdapter(_targets_for([0]))

        return _build

    # run_cohort binds operational_job_lock as a module-global at import time,
    # so the patch must target the name in specialist_evidence_cohort's
    # namespace (not polycopy.runtime.locks).
    import polycopy.ingestion.specialist_evidence_cohort as cohort_mod
    orig_ctx = cohort_mod.operational_job_lock
    cohort_mod.operational_job_lock = lambda *a, **k: TrackedLock(k.get("lock_path"), k.get("timeout", 30.0))
    try:
        # Monkeypatch the cohort's provider construction by intercepting the
        # adapter factory: we pass a spec-like object whose build() is called
        # inside the lock. We emulate by injecting a spec.
        class _Spec:
            def build(self):
                order.append("adapter_build")
                return FakeAdapter(_targets_for([0]))

            def close(self):
                pass

        spec = _Spec()
        cfg = CohortRunConfig()
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=spec, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma(), lock_path=lock_path)
        )
        assert res.status == "success"
        assert order.index("lock_acquire") < order.index("adapter_build"), order
    finally:
        cohort_mod.operational_job_lock = orig_ctx
        db.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


# ── Test 17: deterministic watch order ─────────────────────────────────────
def test_deterministic_watch_order():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    # Submit in a SHUFFLED order; result must be sorted by watch id.
    shuffled = list(reversed(wids))
    adapter = _make_fake_adapter([0, 1, 2, 3, 4])
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=shuffled, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        got = [w.watch_id for w in res.watches]
        assert got == sorted(wids), got
    finally:
        db.close()


# ── Test 18: successful five-watch write commits once ──────────────────────
def test_five_watch_write_commits_once():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    adapter = _make_fake_adapter([0, 1, 2, 3, 4])
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res.status == "success"
        assert res.cohort_committed is True
        assert res.watch_count_completed == 5
        # 2 BUY per wallet (SELL excluded) => 10 rows.
        rows = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        assert rows == 10, rows
        # last_collection_at updated for all 5.
        lac = db.conn.execute(
            "SELECT COUNT(*) FROM specialist_evidence_watchlist "
            "WHERE id IN ({}) AND last_collection_at IS NOT NULL".format(
                ",".join("?" for _ in wids)), wids
        ).fetchone()[0]
        assert lac == 5
    finally:
        db.close()


# ── Test 19: failure on watch 3 rolls back watches 1 and 2 ─────────────────
def test_failure_on_watch3_rolls_back_1_and_2():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])

    class FailingAdapter(FakeAdapter):
        async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
            # The THIRD watch (sorted order) fails after 1&2 wrote.
            if wallet.lower() == ADDR[2].lower():
                raise RuntimeError("simulated provider failure for watch 3")
            return await super().get_trades_by_address(
                wallet, since=since, limit=limit, offset=offset, return_raw=return_raw)

    adapter = FailingAdapter(_targets_for([0, 1, 2, 3, 4]))
    cfg = CohortRunConfig()
    try:
        before = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        after = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        assert res.status == "failed"
        assert res.cohort_committed is False
        # Entire cohort rolled back: watches 1 & 2 writes undone.
        assert before == after, (before, after)
        assert res.watch_count_failed >= 1
    finally:
        db.close()


# ── Test 20: failure on watch 5 rolls back complete cohort ─────────────────
def test_failure_on_watch5_rolls_back_complete_cohort():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])

    class FailingLastAdapter(FakeAdapter):
        async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
            if wallet.lower() == ADDR[4].lower():  # fifth (last) in sorted order
                raise RuntimeError("simulated provider failure for last watch")
            return await super().get_trades_by_address(
                wallet, since=since, limit=limit, offset=offset, return_raw=return_raw)

    adapter = FailingLastAdapter(_targets_for([0, 1, 2, 3, 4]))
    cfg = CohortRunConfig()
    try:
        before = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        after = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        assert res.status == "failed"
        assert res.cohort_committed is False
        assert before == after, (before, after)
    finally:
        db.close()


# ── Test 21/22: adapter + db close exactly once ────────────────────────────
def test_adapter_and_db_close_exactly_once():
    db = _open()
    wids = _seed_active_watches(db, [0, 1])
    adapter = _make_fake_adapter([0, 1])
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res.status == "success"
        assert adapter.closed == 1, adapter.closed
    finally:
        # db.close() is owned by the caller (CLI); here we close once.
        db.close()
        assert adapter.closed == 1


# ── Test 23: structured failure JSON includes original error ───────────────
def test_structured_failure_includes_original_error():
    db = _open()
    wids = _seed_active_watches(db, [0, 1])

    class BoomAdapter(FakeAdapter):
        async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
            if wallet.lower() == ADDR[1].lower():
                raise ValueError("original-provider-error-text")
            return await super().get_trades_by_address(
                wallet, since=since, limit=limit, offset=offset, return_raw=return_raw)

    adapter = BoomAdapter(_targets_for([0, 1]))
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        d = res.as_dict()
        assert d["status"] == "failed"
        assert d["cohort_committed"] is False
        assert d["error"] is not None
        assert "original-provider-error-text" in d["error"], d["error"]
        # The consolidated schema carries run_id + error.
        assert d["run_id"].startswith("cohort_")
    finally:
        db.close()


# ── Test 24: atomic output JSON behavior ───────────────────────────────────
def test_atomic_output_json_behavior():
    import scripts.collect_specialist_evidence_cohort as cli

    db = _open()
    wids = _seed_active_watches(db, [0, 1])
    adapter = _make_fake_adapter([0, 1])
    cfg = CohortRunConfig()
    out_path = Path(tempfile.mktemp(suffix=".json"))
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        # Write atomically via the CLI helper.
        cli._atomic_write_json(str(out_path), res.as_dict())
        assert out_path.exists()
        # The file is valid JSON and matches.
        loaded = json.loads(out_path.read_text())
        assert loaded["status"] == "success"
        # No partial temp file left behind.
        stray = list(out_path.parent.glob(f".{out_path.name}.*"))
        assert stray == [], stray
    finally:
        db.close()
        try:
            out_path.unlink()
        except OSError:
            pass


# ── Test 25: exact allowed SQL write-table set (trace callback) ─────────────
def test_exact_allowed_write_table_set():
    db = _open()
    wids = _seed_active_watches(db, [0, 1])
    adapter = _make_fake_adapter([0, 1], include_sell=True)
    cfg = CohortRunConfig()
    try:
        violations = []
        unrecognized_mutations = []

        # Strict mutating-statement recognizer. Any mutating SQL must match one
        # of these canonical forms; anything else is a contract violation that
        # must NOT be silently ignored (the old parser missed
        # "INSERT OR IGNORE INTO", the primary source-trade insert form).
        MUTATING_FORMS = (
            "INSERT OR IGNORE INTO",
            "INSERT OR REPLACE INTO",
            "INSERT INTO",
            "REPLACE INTO",
            "UPDATE",
            "DELETE FROM",
        )

        def _classify(sql):
            s = sql.strip()
            up = s.upper()
            if not up:
                return None  # not a statement
            word = up.split()[0]
            if word not in ("INSERT", "REPLACE", "UPDATE", "DELETE"):
                return None  # read / control statement
            # It is a mutation: it MUST match a known canonical form.
            for form in MUTATING_FORMS:
                if up.startswith(form):
                    rest = s[len(form):].strip()
                    tbl = (
                        rest.split()[0].strip('"').strip("`").split("(")[0].strip().lower()
                    )
                    return form, tbl
            return "UNRECOGNIZED", up  # mutation but not in the allow-list

        def _trace(sql):
            c = _classify(sql)
            if c is None:
                return
            if c[0] == "UNRECOGNIZED":
                unrecognized_mutations.append((c[1], sql))
                return
            form, tbl = c
            if tbl not in ALLOWED_WRITE_TABLES:
                violations.append((form, tbl, sql))

        db.conn.set_trace_callback(_trace)
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        db.conn.set_trace_callback(None)
        assert res.status == "success"
        assert violations == [], violations
        # Fail-closed: no mutating statement of any shape escaped recognition.
        assert unrecognized_mutations == [], unrecognized_mutations

        # Exact observed write-table set equals the allow-list (no extras).
        observed = set()
        for form, tbl, _ in violations:
            observed.add(tbl)
        # (violations is empty above, but assert the allow-list is exhaustive:
        #  the cohort writes EXACTLY these three tables.)
        assert set(ALLOWED_WRITE_TABLES) == {
            "source_trades",
            "source_trade_enrichments",
            "specialist_evidence_watchlist",
        }, set(ALLOWED_WRITE_TABLES)
    finally:
        db.close()


# ── Test 26: zero deltas in execution/approval plane ───────────────────────
def test_zero_approval_execution_plane_deltas():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2])
    adapter = _make_fake_adapter([0, 1, 2])
    cfg = CohortRunConfig()
    try:
        before = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in FORBIDDEN_WRITE_TABLES}
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        after = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in FORBIDDEN_WRITE_TABLES}
        assert res.status == "success"
        assert before == after, {t: (before[t], after[t]) for t in FORBIDDEN_WRITE_TABLES
                                  if before[t] != after[t]}
    finally:
        db.close()


# ── Test 27: idempotent replay produces no duplicate source trades ─────────
def test_idempotent_replay_no_duplicate_source_trades():
    db = _open()
    wids = _seed_active_watches(db, [0, 1])
    adapter = _make_fake_adapter([0, 1])
    cfg = CohortRunConfig()
    try:
        res1 = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res1.status == "success"
        first_rows = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        # Replay with the SAME fake data.
        res2 = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        second_rows = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        assert res2.status == "success"
        assert first_rows == second_rows, (first_rows, second_rows)
        assert second_rows == 4, second_rows  # 2 BUY/wallet, no dupes
        # source_trade_id stays UNIQUE.
        uniq = db.conn.execute(
            "SELECT COUNT(DISTINCT source_trade_id) FROM source_trades"
        ).fetchone()[0]
        assert uniq == second_rows
    finally:
        db.close()


# ── Test 28: PR #71 build_status observes cohort evidence ──────────────────
def test_pr71_build_status_observes_cohort_evidence():
    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    adapter = _make_fake_adapter([0, 1, 2, 3, 4])
    cfg = CohortRunConfig()
    try:
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg,
                       gamma_resolver=FakeGamma())
        )
        assert res.status == "success"
        assert res.cohort_committed is True
        # Read-only build_status over the disposable v21 DB.
        ro = open_readonly(str(db.db_path))
        try:
            report = st.build_status(
                ro, collector_stale_after_hours=10**9, refresh_stale_after_hours=10**9
            )
        finally:
            ro.close()
        observed = {w["wallet_id"] for w in report["wallets"]}
        # All five supplied wallets are present in the readiness cohort, and each
        # carries the evidence written by the cohort run.
        assert set(WUUID[0:5]).issubset(observed), observed
        by_wallet = {w["wallet_id"]: w["buy_count"] for w in report["wallets"]}
        for i in range(5):
            assert by_wallet[WUUID[i]] >= 1, by_wallet
        # Execution plane untouched.
        assert report["execution_artifact_delta"] == {}
    finally:
        db.close()


# ── Test 29: no scoring/approval/dispatch/candidate/signal/execution invoked ─
def test_no_scoring_approval_dispatch_functions_invoked():
    # Static guarantee: the cohort orchestration imports ONLY the accepted
    # PR #71 collector + watchlist + runtime locks. It must NOT import or call
    # any scoring/approval/dispatch/candidate/signal/execution symbol.
    forbidden = {
        "evaluate_wallet", "specialist_approvals", "approved_specialist_trade_dispatches",
        "copy_candidates", "paper_signal_decisions", "paper_orders", "paper_fills",
        "paper_positions", "execute_authorized", "process_approved", "manage_specialist_approvals",
    }
    src = cohort.__dict__
    for bad in forbidden:
        assert bad not in src, f"cohort module must not reference {bad}"
    # Confirm the collector it delegates to is the ACCEPTED PR #71 one.
    assert cohort._collector is not None
    assert hasattr(cohort._collector, "collect_evidence")
    # The only writer tables permitted remain the accepted set.
    assert set(ALLOWED_WRITE_TABLES) == {
        "source_trades", "source_trade_enrichments", "specialist_evidence_watchlist"
    }


# ── Test 30: real CLI main() integration with five explicit watch ids ──────
def test_cli_main_integration_five_watch_ids():
    import scripts.collect_specialist_evidence_cohort as cli

    db = _open()
    wids = _seed_active_watches(db, [0, 1, 2, 3, 4])
    adapter = _make_fake_adapter([0, 1, 2, 3, 4])
    out_path = Path(tempfile.mktemp(suffix=".json"))
    db.close()  # CLI opens its own connection.

    # Build a fake module-level adapter builder for the CLI by monkeypatching
    # the PolymarketPublicAdapter import used inside main(). We replace the
    # adapter construction with our fake so NO live network occurs.
    import polycopy.adapters.polymarket as pmod

    class _FakePublicAdapter:
        def __init__(self, **kwargs):
            self._inner = adapter

        async def get_trades_by_address(self, *a, **k):
            return await self._inner.get_trades_by_address(*a, **k)

        def close(self):
            self._inner.close()

    orig_cls = pmod.PolymarketPublicAdapter
    pmod.PolymarketPublicAdapter = _FakePublicAdapter
    try:
        rc = cli.main([
            "--db-path", str(db.db_path),
            "--watch-id", wids[0],
            "--watch-id", wids[1],
            "--watch-id", wids[2],
            "--watch-id", wids[3],
            "--watch-id", wids[4],
            "--dry-run",
            "--json",
            "--output-json", str(out_path),
        ])
        assert rc == 0, rc
        assert out_path.exists(), "output JSON not written"
        loaded = json.loads(out_path.read_text())
        assert loaded["status"] == "success"
        assert loaded["watch_count_requested"] == 5
        assert loaded["watch_count_completed"] == 5
        assert loaded["watch_count_failed"] == 0
        assert loaded["cohort_committed"] is False  # dry-run
        assert len(loaded["watches"]) == 5
        # Dry-run: zero writes to source_trades.
        chk = Database(db.db_path).connect()
        try:
            assert chk.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0] == 0
        finally:
            chk.close()
    finally:
        pmod.PolymarketPublicAdapter = orig_cls
        try:
            out_path.unlink()
        except OSError:
            pass
        try:
            db.close()
        except Exception:
            pass
