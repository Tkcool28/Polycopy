"""PR #73 corrections — end-to-end proof battery (isolated, no production).

Every test uses disposable temp DBs ONLY. Nothing here opens /root/Polycopy's
production DB, deploys, migrates, or starts services/canaries. These tests
prove the 8 required corrections and the 21 final-validation bullets.

Run:
  PYTHONPATH=src:scripts python -m pytest tests/test_pr73_corrections.py -q
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.specialist_evidence_cohort import (  # noqa: E402
    CohortRunConfig,
    run_cohort,
    ALLOWED_WRITE_TABLES,
    FORBIDDEN_WRITE_TABLES,
)
import polycopy.ingestion.specialist_evidence_watchlist as wl  # noqa: E402

COND = ["0x" + c * 64 for c in ("a", "b", "c", "d", "e", "f")]
TOK = ["0x" + c * 64 for c in ("a", "b", "c", "d", "e", "f")]
ADDR = ["0xgood0000000000000000000000000000000000" + c + c for c in ("a", "b", "c", "d", "e", "f")]
WUUID = ["uuid-wallet-0000000000000000000000000000000" + c for c in ("a", "b", "c", "d", "e", "f")]


def _open():
    p = Path(tempfile.mktemp(suffix=".db"))
    return Database(p).connect()


def _seed_wallet(db, wid, address, is_sample=0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", is_sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _seed(db, indices):
    wids = []
    for i in indices:
        _seed_wallet(db, WUUID[i], ADDR[i])
        wids.append(wl.add_watch(db, wallet_id=WUUID[i]))
    return wids


def _buy(tid, cond, tok, addr):
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


def _sell(tid, cond, tok, addr):
    r = _buy(tid, cond, tok, addr)
    r["side"] = "SELL"
    return r


# ── Adapters ────────────────────────────────────────────────────────────────
class FakeAdapter:
    """Production-shaped fake: ONLY async aclose(); no sync close()."""

    def __init__(self, targets=None, *, get_market_raw=None):
        self._targets = targets or {}
        self._gmr = get_market_raw or (lambda c: {"conditionId": c, "category": "Politics"})
        self.aclose_calls = 0
        self.get_trades_calls = 0

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        self.get_trades_calls += 1
        return list(self._targets.get(wallet.lower(), []))[:limit]

    def get_market_raw(self, condition_id):
        return self._gmr(condition_id)

    async def aclose(self):
        self.aclose_calls += 1


class FailingOnWatch3Adapter(FakeAdapter):
    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        if wallet.lower() == ADDR[2].lower():
            raise RuntimeError("simulated provider failure on watch 3")
        return await super().get_trades_by_address(
            wallet, since=since, limit=limit, offset=offset, return_raw=return_raw
        )


def _targets(indices, rows_per=2, include_sell=False):
    t = {}
    for i in indices:
        rows = [_buy(f"t{i}_{j}", COND[i], TOK[i], ADDR[i]) for j in range(rows_per)]
        if include_sell:
            rows.append(_sell(f"s{i}", COND[i], TOK[i], ADDR[i]))
        t[ADDR[i].lower()] = rows
    return t


def _count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 1 — writer/uniqueness failure → full cohort rollback
# ═════════════════════════════════════════════════════════════════════════════
def test_writer_failure_full_cohort_rollback():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    adapter = FailingOnWatch3Adapter(_targets([0, 1, 2, 3, 4]))
    cfg = CohortRunConfig()

    before = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES + FORBIDDEN_WRITE_TABLES}

    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )

    after = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES + FORBIDDEN_WRITE_TABLES}

    # Assertions required by the PR.
    assert res.status == "failed"
    assert res.cohort_committed is False
    # Original writer/provider error is present and preserved verbatim.
    assert "RuntimeError" in (res.error or "")
    assert "simulated provider failure on watch 3" in (res.error or "")
    # Zero delta everywhere (full rollback of staged watches 1 and 2 too).
    assert before == after, {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    # Watchlist changes for watches 1 & 2 rolled back (last_collection_at NULL).
    n_updated = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_evidence_watchlist "
        "WHERE last_collection_at IS NOT NULL"
    ).fetchone()[0]
    assert n_updated == 0, n_updated
    # Watches after the failing one are marked unprocessed and never called.
    statuses = [w.status for w in res.watches]
    fail_idx = next((i for i, s in enumerate(statuses) if s == "error"), None)
    assert fail_idx is not None
    assert all(s == "unprocessed" for s in statuses[fail_idx + 1:]), statuses
    # Adapter closed exactly once.
    assert adapter.aclose_calls == 1, adapter.aclose_calls
    db.close()


def test_uniqueness_preflight_failure_rolls_back():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    import polycopy.ingestion.specialist_evidence_collector as coll
    from polycopy.ingestion.source_trade_writer import WriteResult

    cfg = CohortRunConfig()
    adapter = FakeAdapter(_targets([0, 1, 2]))

    orig_collect = coll.collect_evidence
    call = {"n": 0}

    async def _coll_patch(db_, **kw):
        call["n"] += 1
        if call["n"] == 2:
            wr = WriteResult(errors=1, rolled_back=False,
                             unique_constraint_present=False)
            raise coll.WriterFailure(str(wr.errors), stop_reason="writer_failure")
        return await orig_collect(db_, **kw)

    coll.collect_evidence = _coll_patch
    try:
        before = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES}
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
        )
        after = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES}
        assert res.status == "failed"
        assert res.cohort_committed is False
        assert before == after
        assert adapter.aclose_calls == 1
    finally:
        coll.collect_evidence = orig_collect
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 2 — cohort-wide honest bounds
# ═════════════════════════════════════════════════════════════════════════════
def test_record_budget_is_cohort_wide():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    adapter = FakeAdapter(_targets([0, 1, 2, 3, 4], rows_per=10))
    cfg = CohortRunConfig(max_total_new_trades=12)
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    created = _count(db, "source_trades")
    assert res.status in ("success", "failed", "partial")
    assert created == 12, (created, res.as_dict())
    assert res.totals["rows_created"] == 12
    assert res.remaining["max_total_new_trades"] == 0
    assert adapter.aclose_calls == 1
    db.close()


def test_gamma_budget_exhaustion_is_cohort_wide():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    adapter = FakeAdapter(_targets([0, 1, 2, 3, 4]),
                          get_market_raw=lambda c: {"conditionId": c, "category": "Politics"})
    cfg = CohortRunConfig(max_gamma_requests=2, resolve_gamma=True)
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    assert res.status == "failed"
    assert res.stop_reason == "gamma_budget_exhausted"
    assert res.consumption["gamma_requests"] == 2, res.consumption
    assert res.remaining["max_gamma_requests"] == 0
    assert res.limits["max_gamma_requests"] == 2
    assert adapter.aclose_calls == 1
    db.close()


def test_deadline_expiry_rolls_back():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    adapter = FakeAdapter(_targets([0, 1, 2], rows_per=5))
    cfg = CohortRunConfig(timeout_seconds=-1.0, enforce_deadline=True)

    before = _count(db, "source_trades")
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    after = _count(db, "source_trades")
    assert res.status == "failed"
    assert res.stop_reason == "deadline_exceeded", res.stop_reason
    assert res.cohort_committed is False
    assert before == after
    assert adapter.aclose_calls == 1
    db.close()


def test_rss_limit_enforced_fail_closed():
    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1]))
    cfg = CohortRunConfig(rss_mb_limit=1.0)

    import polycopy.ingestion.specialist_evidence_collector as coll
    orig_sample = coll._rss_mb

    def _over():
        return 10_000.0

    coll._rss_mb = _over
    try:
        before = _count(db, "source_trades")
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
        )
        after = _count(db, "source_trades")
        assert res.status == "failed"
        assert res.stop_reason == "rss_limit_exceeded", res.stop_reason
        assert res.cohort_committed is False
        assert before == after
        assert adapter.aclose_calls == 1
    finally:
        coll._rss_mb = orig_sample
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 3 — Gamma resolved once per unique condition; replay zero-write
# ═════════════════════════════════════════════════════════════════════════════
def test_gamma_resolved_once_and_replay_zero_write():
    db = _open()
    wids = _seed(db, [0, 1])
    gmr_calls = []

    def _gmr(cond):
        gmr_calls.append(cond)
        return {"conditionId": cond, "category": "Politics", "tags": ["election"]}

    adapter = FakeAdapter(_targets([0, 1]), get_market_raw=_gmr)
    cfg = CohortRunConfig(resolve_gamma=True, max_gamma_requests=100)

    res1 = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    assert res1.status == "success"
    first_gamma = len(gmr_calls)
    assert first_gamma == 2, first_gamma

    res2 = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    assert res2.status == "success"
    # Each run creates a fresh dedupe cache, so Gamma is re-resolved per run
    # (deterministic: 2 calls/run). The idempotent replay writes ZERO new rows.
    second_gamma = len(gmr_calls) - first_gamma
    assert second_gamma == 2, second_gamma
    # Zero delta in source_trades (idempotent INSERT OR IGNORE).
    assert _count(db, "source_trades") == 4, _count(db, "source_trades")
    assert adapter.aclose_calls == 2
    db.close()


def test_gamma_failure_causes_rollback():
    db = _open()
    wids = _seed(db, [0, 1])

    def _gmr_fail(cond):
        raise RuntimeError("gamma down")

    adapter = FakeAdapter(_targets([0, 1]), get_market_raw=_gmr_fail)
    cfg = CohortRunConfig(resolve_gamma=True, max_gamma_requests=100)
    before = _count(db, "source_trades")
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    after = _count(db, "source_trades")
    assert res.status == "failed"
    # A Gamma failure is a cohort failure (rollback). stop_reason may be a
    # gamma-specific code or a generic enrichment error — both are hard stops.
    assert res.stop_reason in ("gamma_resolution_error", "enrich_error", "gamma_budget_exhausted")
    assert before == after
    assert adapter.aclose_calls == 1
    db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 4 — async aclose() on every exit path
# ═════════════════════════════════════════════════════════════════════════════
def test_async_aclose_on_every_exit_path():
    cases = {}

    db = _open()
    wids = _seed(db, [0, 1])
    a = FakeAdapter(_targets([0, 1]))
    asyncio.run(run_cohort(db, watch_ids=wids, adapter=a, dry_run=False, config=CohortRunConfig()))
    cases["success"] = a.aclose_calls
    db.close()

    db = _open()
    wids = _seed(db, [0, 1, 2])
    a = FailingOnWatch3Adapter(_targets([0, 1, 2]))
    asyncio.run(run_cohort(db, watch_ids=wids, adapter=a, dry_run=False, config=CohortRunConfig()))
    cases["watch_fail"] = a.aclose_calls
    db.close()

    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    a = FakeAdapter(_targets([0, 1, 2, 3, 4]))
    asyncio.run(run_cohort(db, watch_ids=wids, adapter=a, dry_run=False,
                           config=CohortRunConfig(max_gamma_requests=1, resolve_gamma=True)))
    cases["gamma"] = a.aclose_calls
    db.close()

    db = _open()
    wids = _seed(db, [0, 1, 2])

    class BuildFailSpec:
        def build(self):
            raise RuntimeError("adapter.build() exploded")

        def close(self):
            pass

    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=BuildFailSpec(), dry_run=False, config=CohortRunConfig()))
    assert res.status == "failed"
    db.close()

    for k, v in cases.items():
        assert v == 1, (k, v)


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 5 — structured failure on provider build() failure
# ═════════════════════════════════════════════════════════════════════════════
def test_provider_build_failure_structured():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    built = {"n": 0}

    class BuildFailSpec:
        def build(self):
            built["n"] += 1
            raise ConnectionError("cannot construct provider")

        def close(self):
            pass

    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=BuildFailSpec(), dry_run=False, config=CohortRunConfig())
    )
    assert res.status == "failed"
    assert res.cohort_committed is False
    assert "ConnectionError" in (res.error or "")
    assert "cannot construct provider" in (res.error or "")
    assert res.watch_count_requested == 3
    assert res.watch_count_processed == 0
    assert built["n"] == 1
    db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 6 — invalid watch sets rejected before writable open (CLI)
# ═════════════════════════════════════════════════════════════════════════════
def test_cli_invalid_watch_sets_before_writable_open():
    import scripts.collect_specialist_evidence_cohort as cli
    import evidence_db

    calls = {"writable": 0}

    def _open_writable_fail(*a, **k):
        calls["writable"] += 1
        raise AssertionError("open_writable must NOT be called for invalid input")

    orig_open = evidence_db.open_writable
    evidence_db.open_writable = _open_writable_fail
    try:
        invalid = [
            [],
            [f"w{i}" for i in range(6)],
            ["bad id with spaces"],
            ["dup", "dup"],
        ]
        for ws in invalid:
            argv = ["--db-path", ":memory:", "--dry-run"]
            for w in ws:
                argv += ["--watch-id", w]
            rc = cli.main(argv)
            assert rc == 2, (ws, rc)
        assert calls["writable"] == 0, calls
    finally:
        evidence_db.open_writable = orig_open


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 7 — strict SQL write-table set
# ═════════════════════════════════════════════════════════════════════════════
def test_exact_observed_write_table_set():
    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1], include_sell=True))
    MUTATING_FORMS = (
        "INSERT OR IGNORE INTO", "INSERT OR REPLACE INTO", "INSERT INTO",
        "REPLACE INTO", "UPDATE", "DELETE FROM",
    )
    unrecognized = []
    observed = set()

    def _classify(sql):
        s = sql.strip()
        up = s.upper()
        if not up:
            return None
        if up.split()[0] not in ("INSERT", "REPLACE", "UPDATE", "DELETE"):
            return None
        for form in MUTATING_FORMS:
            if up.startswith(form):
                tbl = s[len(form):].strip().split()[0].strip('"').strip("`").split("(")[0].strip().lower()
                return form, tbl
        return "UNRECOGNIZED", up

    def _trace(sql):
        c = _classify(sql)
        if c is None:
            return
        if c[0] == "UNRECOGNIZED":
            unrecognized.append(sql)
            return
        observed.add(c[1])

    db.conn.set_trace_callback(_trace)
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig()))
    db.conn.set_trace_callback(None)
    assert res.status == "success"
    assert unrecognized == [], unrecognized
    assert observed == set(ALLOWED_WRITE_TABLES), observed
    db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 8 — truthful metrics on every exit path
# ═════════════════════════════════════════════════════════════════════════════
def test_truthful_metrics_dry_run_and_write_and_replay():
    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1]))
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=CohortRunConfig()))
    assert res.status == "success"
    assert res.cohort_committed is False
    assert res.totals["rows_created"] == 0
    assert res.totals["rows_would_create"] == 4, res.totals
    assert _count(db, "source_trades") == 0
    db.close()

    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1]))
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig()))
    assert res.status == "success"
    assert res.cohort_committed is True
    assert res.totals["rows_created"] == 4, res.totals
    assert res.totals["rows_would_create"] == 0
    assert res.watch_count_processed == 2
    assert res.watch_count_failed == 0
    assert res.watch_count_unprocessed == 0
    db.close()


def test_truthful_metrics_writer_failure():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    adapter = FailingOnWatch3Adapter(_targets([0, 1, 2]))
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig()))
    assert res.status == "failed"
    # unprocessed watches are NOT counted as completed; processed+failed+unprocessed
    # must equal the requested count.
    assert res.watch_count_processed + res.watch_count_failed + res.watch_count_unprocessed == 3
    assert res.watch_count_failed == 1
    assert res.totals["rows_created"] == 0
    db.close()
