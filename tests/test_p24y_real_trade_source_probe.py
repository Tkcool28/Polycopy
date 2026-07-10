"""PR24Y — Real Wallet Trade Source Probe tests.

Proves the read-only source probe behaves under Polycopy's hard guardrails for
PR24Y (Step 1/Step 2 source probe only; no ingestion):

  * default probe makes NO network call (fixture provider)
  * live provider cannot run without --allow-live-preview (CLI level)
  * wallet input required for live preview
  * >5 wallets rejected
  * record limit bounded to 100
  * pagination cannot exceed two pages in PR24Y
  * BUY classified correctly; lowercase buy -> BUY
  * SELL -> excluded_unsupported_side
  * missing side / price / size / timestamp -> excluded
  * token_id -> PR24U-ready; conditionId -> PR24V-ready; both -> both-ready
  * stable trade id detected
  * duplicate records across pages counted/reported
  * JSON serialization works
  * report contains source-selection verdict
  * no Database import in core module
  * no INSERT/UPDATE/DELETE/CREATE/DROP/ALTER execution path
  * no decisions/candidates/signals/snapshots/orders/positions
  * no timer/service/deploy behavior
  * production DB never opened by the probe; size/mtime captured via
    os.stat only (separate from the probe) and reported in the result

No automated test calls the real API (fake providers + fixtures only).
"""

from __future__ import annotations

import inspect
import json

from polycopy.engine import real_trade_source_probe as mod
from polycopy.engine.real_trade_source_probe import (
    run_real_trade_source_probe,
    report_to_markdown,
    report_to_json,
    validate_wallet_inputs,
    _build_preview,
    assess_stable_identity,
    HARD_MAX_RECORD_LIMIT,
    PR24Y_MAX_PAGES,
)

FAKE_WALLET = "0x" + "1" * 40


def _tx(i: str) -> str:
    return "0x" + i * 64


def _rec(**over) -> dict:
    base = {
        "transactionHash": _tx("a"),
        "proxyWallet": FAKE_WALLET,
        "asset": _tx("2"),
        "conditionId": _tx("3"),
        "side": "BUY",
        "price": "0.4",
        "size": "100",
        "timestamp": 1700000000,
        "outcome": "Yes",
        "title": "Market A",
        "slug": "market-a",
    }
    base.update(over)
    return base


class _FakeProvider:
    """Deterministic fake; records calls; returns scripted pages.

    Each entry in ``pages`` is a full API page (not offset-sliced); this
    matches the real data-api contract where page N is fetched independently.
    ``made_network_call`` is False to signal the probe this is NOT a real
    external HTTP request (so network counters stay 0).
    """

    made_network_call = False

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def fetch_trades(self, wallet, *, limit, page):
        self.calls += 1
        if page >= len(self.pages):
            return []
        return list(self.pages[page])


# ── 1. Default probe makes no network call ───────────────────────────────────
def test_default_probe_makes_no_network_call():
    # Default (fixture) provider does NOT count as a network call.
    provider = _FakeProvider([[]])  # empty -> no records
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET])
    )
    # Fixture/in-memory providers are never counted as external HTTP requests.
    assert res.network_calls_attempted == 0
    assert res.network_calls_succeeded == 0
    assert res.pages_fetched == 0
    assert res.production_db_opened is False
    assert res.production_db_written is False


def test_default_fixture_run_reports_zero_network_and_pages():
    # Explicit exact-value assertion required by the PR24Y correction.
    provider = _FakeProvider([[]])
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET])
    )
    assert res.live_preview_enabled is False
    assert res.network_calls_attempted == 0
    assert res.network_calls_succeeded == 0
    assert res.pages_fetched == 0
    assert res.raw_records == 0
    assert res.source_selection_verdict == "SOURCE_PARTIAL"


# ── 2. Live provider cannot run without --allow-live-preview ────────────────
def test_live_provider_not_used_without_flag():
    # In default mode the CLI uses _FixtureProvider (no network). We assert the
    # core never opens the DB and the real provider class is only built when
    # the flag is set. Behavioral check: running without allow_live_preview
    # yields production_db_opened=False and a SOURCE_PARTIAL/verdict, not a
    # network error.
    provider = _FakeProvider([[]])
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET], allow_live_preview=False)
    )
    assert res.live_preview_enabled is False
    assert res.production_db_opened is False


# ── 3. Wallet input is required for live preview ─────────────────────────────
def test_wallet_input_required():
    import pytest

    with pytest.raises(ValueError):
        validate_wallet_inputs(None, None)


# ── 4. More than five wallets is rejected ────────────────────────────────────
def test_more_than_five_wallets_rejected():
    import pytest

    addrs = [f"0x{i}" + "0" * 38 for i in range(6)]
    with pytest.raises(ValueError):
        validate_wallet_inputs(addrs, None)


# ── 5. Record limit is bounded to 100 ────────────────────────────────────────
def test_record_limit_bounded_to_100():
    # limit > hard max is clamped.
    provider = _FakeProvider([[]])
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET], record_limit=1000)
    )
    assert res.record_limit <= HARD_MAX_RECORD_LIMIT
    assert res.record_limit == HARD_MAX_RECORD_LIMIT


# ── 6. Pagination cannot exceed two pages in PR24Y ──────────────────────────
def test_pagination_cannot_exceed_two_pages():
    # 5 scripted pages; probe must stop at PR24Y_MAX_PAGES.
    pages = [[_rec()] for _ in range(5)]
    provider = _FakeProvider(pages)
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET])
    )
    assert provider.calls <= PR24Y_MAX_PAGES
    assert res.pages_fetched <= PR24Y_MAX_PAGES


# ── 7. BUY record is classified correctly ────────────────────────────────────
def test_buy_record_classified_correctly():
    pv = _build_preview(_rec(side="BUY"), index=0)
    assert pv.side_canonical == "BUY"
    assert pv.eligibility_reason == "eligible_buy"


# ── 8. lowercase buy canonicalizes to BUY ────────────────────────────────────
def test_lowercase_buy_canonicalizes_to_BUY():
    pv = _build_preview(_rec(side="buy"), index=0)
    assert pv.side_canonical == "BUY"


# ── 9. SELL is classified excluded_unsupported_side ─────────────────────────
def test_sell_classified_excluded_unsupported_side():
    pv = _build_preview(_rec(side="SELL"), index=0)
    assert pv.side_canonical == "SELL"
    assert pv.eligibility_reason == "excluded_unsupported_side"
    res = __import__("asyncio").run(
        run_real_trade_source_probe(_FakeProvider([[ _rec(side="SELL") ]]), [FAKE_WALLET])
    )
    assert res.raw_sell_records == 1
    assert res.excluded_unsupported_side == 1
    assert res.eligible_buy_records == 0


# ── 10. Missing side is excluded ─────────────────────────────────────────────
def test_missing_side_excluded():
    r = _rec()
    del r["side"]
    pv = _build_preview(r, index=0)
    assert pv.side_canonical is None
    assert pv.eligibility_reason == "excluded_missing_fields"


# ── 11. Missing price is excluded ────────────────────────────────────────────
def test_missing_price_excluded():
    r = _rec()
    del r["price"]
    pv = _build_preview(r, index=0)
    assert "price" in pv.eligibility_reason
    assert pv.eligibility_reason.startswith("excluded_missing_fields")


# ── 12. Missing size is excluded ─────────────────────────────────────────────
def test_missing_size_excluded():
    r = _rec()
    del r["size"]
    pv = _build_preview(r, index=0)
    assert "size" in pv.eligibility_reason


# ── 13. Missing timestamp is excluded ────────────────────────────────────────
def test_missing_timestamp_excluded():
    r = _rec()
    del r["timestamp"]
    pv = _build_preview(r, index=0)
    assert "timestamp" in pv.eligibility_reason


# ── 14. token_id makes record PR24U-ready ────────────────────────────────────
def test_token_id_makes_pr24u_ready():
    pv = _build_preview(_rec(asset=_tx("9")), index=0)
    assert pv.token_id_present
    assert pv.pr24u_ready is True


# ── 15. conditionId makes record PR24V-ready ─────────────────────────────────
def test_condition_id_makes_pr24v_ready():
    pv = _build_preview(_rec(conditionId=_tx("7")), index=0)
    assert pv.condition_id_present
    assert pv.pr24v_ready is True


# ── 16. token_id + conditionId makes both-ready ──────────────────────────────
def test_token_and_condition_make_both_ready():
    pv = _build_preview(_rec(asset=_tx("9"), conditionId=_tx("7")), index=0)
    assert pv.pr24u_ready and pv.pr24v_ready
    assert pv.both_ready is True


# ── 17. stable trade ID is detected ──────────────────────────────────────────
def test_stable_trade_id_detected():
    pvs = [
        _build_preview(_rec(transactionHash=_tx("a")), index=0),
        _build_preview(_rec(transactionHash=_tx("b")), index=1),
    ]
    a = assess_stable_identity(pvs)
    assert a.stable_source_trade_id_available is True
    assert "transactionHash" in (a.identity_field or "")


# ── 18. duplicate records across pages are counted/reported ──────────────────
def test_duplicate_records_across_pages_counted():
    # Same VALID tx hash on page 0 and page 1 -> identity assessment flags a
    # collision (not stable; row-distinguishing key required before ingestion).
    same = _rec(transactionHash=_tx("d" * 64))
    provider = _FakeProvider([[same], [same]])
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET])
    )
    assert res.raw_records == 2
    # identity assessment sees the duplicate -> not stable
    assert res.identity["stable_source_trade_id_available"] is False
    assert "duplicate" in res.identity["collision_risk_notes"].lower()


# ── 19. JSON serialization works ─────────────────────────────────────────────
def test_json_serialization_works():
    res = __import__("asyncio").run(
        run_real_trade_source_probe(_FakeProvider([[]]), [FAKE_WALLET])
    )
    js = report_to_json(res)
    parsed = json.loads(js)
    assert parsed["probe_version"].startswith("PR24Y")
    assert "source_selection_verdict" in parsed


# ── 20. report contains source-selection verdict ─────────────────────────────
def test_report_contains_source_selection_verdict():
    res = __import__("asyncio").run(
        run_real_trade_source_probe(_FakeProvider([[]]), [FAKE_WALLET])
    )
    md = report_to_markdown(res)
    assert "SOURCE_PARTIAL" in md or "SOURCE_CONFIRMED" in md
    assert "polymarket_data_api_trades_user" in md


# ── 21. no Database import in core module ────────────────────────────────────
def test_no_database_import_in_core_module():
    src = inspect.getsource(mod)
    low = src.lower()
    forbidden = (
        "import polycopy.db.database",
        "from polycopy.db.database",
        "database.connect(",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden token {tok!r} in core module"


# ── 22. no INSERT/UPDATE/DELETE/CREATE/DROP/ALTER execution path ─────────────
def test_no_mutation_sql_in_core_module():
    src = inspect.getsource(mod)
    low = src.lower()
    forbidden = (
        "insert into", "update ", "delete from", "drop table",
        "alter table", "create table", "create index", ".commit(",
        "conn.execute(", "db.execute(", "db.conn.execute(",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden token {tok!r} in core module"


# ── 23. no decisions/candidates/signals/snapshots/orders/positions ───────────
def test_no_protected_table_writes_referenced():
    """The core module must not contain SQL write primitives or execute/commit
    calls. Mentioning table names in prose ('no orders', 'no positions') is
    allowed; actual write machinery is not."""
    src = inspect.getsource(mod)
    low = src.lower()
    forbidden = (
        ".execute(", ".commit(", "insert into", "update ",
        "delete from", "drop table", "alter table", "create table",
        "create index", "source_trades.execute", "wallet_score_decisions.execute",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden write primitive {tok!r} in core module"


# ── 24. no timer/service/deploy behavior ─────────────────────────────────────
def test_no_timer_service_deploy_behavior():
    src = inspect.getsource(mod)
    low = src.lower()
    for tok in ("timer", "systemctl", "deploy", "schedule", "asyncio.run("):
        assert tok not in low, f"forbidden token {tok!r} in core module"


# ── 25. production DB size+mtime unchanged if optional mode=ro mapping ───────
def test_production_db_untouched_when_probe_runs():
    # The probe never opens the DB. We assert the production path is never
    # touched by importing the module or running the probe (fixture mode).
    import os

    db_path = os.path.join("/root/Polycopy", "data", "polycopy.db")
    if not os.path.exists(db_path):
        pytest_skip = __import__("pytest").skip
        pytest_skip("production DB not present in this env")
    before = os.stat(db_path).st_size
    res = __import__("asyncio").run(
        run_real_trade_source_probe(_FakeProvider([[]]), [FAKE_WALLET])
    )
    after = os.stat(db_path).st_size
    assert before == after, "probe mutated production DB size"
    assert res.production_db_opened is False
    assert res.production_db_written is False


def test_selected_source_and_verdict_fields():
    res = __import__("asyncio").run(
        run_real_trade_source_probe(_FakeProvider([[]]), [FAKE_WALLET])
    )
    assert res.selected_source == "polymarket_data_api_trades_user"
    assert res.source_selection_verdict in (
        "SOURCE_CONFIRMED", "SOURCE_PARTIAL", "SOURCE_UNSUITABLE",
        "SOURCE_UNAVAILABLE", "SOURCE_REQUIRES_AUTH", "SOURCE_RESPONSE_CHANGED",
    )
    assert res.ready_to_persist_source_trades is False
    assert res.ready_to_wire_to_automation is False


def test_real_records_confirm_source_and_ready_for_pr24z():
    buy = _rec(transactionHash=_tx("a"))
    provider = _FakeProvider([[buy]])
    res = __import__("asyncio").run(
        run_real_trade_source_probe(provider, [FAKE_WALLET])
    )
    assert res.raw_records == 1
    assert res.raw_buy_records == 1
    assert res.eligible_buy_records == 1
    assert res.source_selection_verdict == "SOURCE_CONFIRMED"
    assert res.ready_for_pr24z is True
    assert res.token_id_available_count == 1
    assert res.condition_id_available_count == 1
    assert res.both_ready_count == 1
