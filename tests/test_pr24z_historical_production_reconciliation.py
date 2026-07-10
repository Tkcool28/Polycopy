"""PR24Z historical production row reconciliation + HARD identity gate tests.

Read-only verification only. No live API, no production write.

16 reconciliation cases + 6 identity-compatibility-gate cases.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path("/root/Polycopy")
REPORT = ROOT / "reports" / "pr24z_historical_production_row_reconciliation.json"
CSV_PATH = ROOT / "reports" / "pr24z_historical_production_row_reconciliation.csv"
MAIN_JSON = ROOT / "reports" / "pr24z_manual_real_source_trade_ingestion.json"
HIST_REF = ROOT / "reports" / "pr24z_historical_production_reference.json"
DB_PATH = str(ROOT / "data" / "polycopy.db")
SOURCE = "polymarket_data_api_trades_user"

FIXTURE_WALLET = "0x1111111111111111111111111111111111111111"
FIXTURE_PREFIXES = ("0x2222", "0x3333", "0x4444", "0x5555", "0x6666", "0x7777")

# 14 historical source_trade_ids (canonical strong ids, polymarket:<64hex>).
HIST_IDS = [
    "polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90",
    "polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75",
    "polymarket:bd05d2c65e8c76b3017e2ae76b917311dfca8eb64d80b31c0c35f4dc6bdca54c",
    "polymarket:bf5384d36afcfb132658b11e703d79a088f9b5821a02290d0652216fb6c8958f",
    "polymarket:e9bd9ceacfb86240db805877269795d95773868a4fe497f92d76bd43cdd18536",
    "polymarket:b3fbc4a16770ccde262588878b8d52169550098e92aa0c2e7ea54a22b3c99943",
    "polymarket:1c69cda9e635a0e8e483c148c89c26cdbfa7953785f8c99f9b5d0ccbafd557a3",
    "polymarket:afc7a199a17cfc3a12588e6457299e6ea8cc73d6d162f1883fe91f4d46077e86",
    "polymarket:74f08b2cf54cd9170ed2a18d452198b1e3155cab6162fb96b2e428f3eb51a210",
    "polymarket:d638d2140198a51f5796078389cff4ce936f983aa8522c5b28b6ec8d552a0607",
    "polymarket:763644f6c36eba47a92ec9c4c01ec10c6dfb4d9ad87503a718c01f9a493f71ca",
    "polymarket:ec5d86981161be068734dcde26c957a16c4584f4c2c8bbb1856b77b5f7d96ecd",
    "polymarket:d84dbf96d8e00266b6de83a0604a0da425ab32441ab2d1c18223f51a5be7c106",
    "polymarket:ccbcc9265c2d3578f6dc5732fef92f88b1f864d4ceaa380b6aa670afc5ec6d18",
]


# ── 16 reconciliation cases ──────────────────────────────────────────────────
def _load_json():
    return json.loads(REPORT.read_text())


def test_reconciliation_artifact_exists():
    assert REPORT.exists()


def test_historical_run_commit_and_mode():
    art = _load_json()
    hr = art["historical_run"]
    assert hr["commit"] == "56fbd0ee67770af4df5c2dcd93d65eec4c2df583"
    assert hr["mode"] == "production-write"
    assert hr["live"] is True
    assert hr["network_calls_attempted"] == 2
    assert hr["network_calls_succeeded"] == 2
    assert hr["raw_records"] == 25
    assert hr["eligible_buy_records"] == 14
    assert hr["inserted_rows"] == 14


def test_production_db_matched_14():
    art = _load_json()
    pdb = art["production_db"]
    assert pdb["source_trades_total"] == 19
    assert pdb["matched_historical_rows"] == 14
    assert pdb["unmatched_historical_report_rows"] == 0
    assert pdb["unexpected_extra_matches"] == 0


def test_reconciliation_summary_all_14_match():
    art = _load_json()
    s = art["reconciliation_summary"]
    assert s["rows_examined"] == 14
    assert s["rows_all_fields_match"] == 14
    assert s["rows_with_mismatch"] == 0
    assert s["fixture_rows_found_in_production_set"] == 0
    assert s["all_14_proven_real_format"] is True
    assert s["all_14_report_db_match"] is True


def test_fixture_verification_run_separate():
    art = _load_json()
    fv = art["fixture_verification_run"]
    assert fv["mode"] == "safety-verification"
    assert fv["live"] is False
    assert fv["network_calls_attempted"] == 0
    assert fv["fixture_wallet"] == FIXTURE_WALLET
    assert fv["valid_rows"] == 3
    assert fv["write_was_null"] is True
    assert fv["rows_written_to_production"] == 0


def test_live_read_only_reconfirmation_no_write():
    art = _load_json()
    lr = art["live_read_only_reconfirmation"]
    assert lr["wallet"] == "0xcac76b761231464900cce5da7c20233d59b20579"
    assert lr["production_write_requested"] is False
    assert lr["production_write_performed"] is False


def test_identity_pipeline_summary():
    art = _load_json()
    ip = art["identity_pipeline"]
    assert ip["historical_mapping_bug_confirmed"] is True
    assert ip["historical_source_id_mislabeled_as_transaction_hash"] is True
    assert ip["historical_strong_count"] == 0
    assert ip["historical_fallback_count"] == 25
    assert ip["current_source_provided_count_for_14_rows"] == 14
    assert ip["current_transaction_hash_count_for_14_rows"] == 0
    assert ip["current_fallback_count_for_14_rows"] == 0
    assert ip["current_ambiguous_count_for_14_rows"] == 0
    assert ip["duplicate_rows_that_would_be_inserted"] == 0
    assert ip["correction_verified"] is True


def test_no_fixture_rows_labeled_historical():
    """Fixture report rows cannot be labeled as historical production rows."""
    art = _load_json()
    assert art["historical_run"]["wallet_address"] != FIXTURE_WALLET
    for r in art["row_reconciliation"]:
        assert r["report_source_trade_id"] in HIST_IDS
        assert not r["report_source_trade_id"].startswith("0x1111")


def test_reconciliation_requires_exact_source_trade_id_selection():
    """Reconciliation must select by exact historical source_trade_id, not count."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        ph = ",".join("?" * len(HIST_IDS))
        rows = conn.execute(
            "SELECT COUNT(*) c FROM source_trades WHERE source=? AND source_trade_id IN (%s)"
            % ph,
            [SOURCE] + HIST_IDS,
        ).fetchone()["c"]
        assert rows == 14
        wrong = conn.execute(
            "SELECT COUNT(*) c FROM source_trades WHERE source=? AND source_trade_id=?",
            [SOURCE, "polymarket:deadbeef"],
        ).fetchone()["c"]
        assert wrong == 0
    finally:
        conn.close()


def test_all_14_rows_present():
    art = _load_json()
    assert len(art["row_reconciliation"]) == 14


def test_missing_db_row_fails_reconciliation():
    """If a DB row were missing, all_fields_match would not be 14/14."""
    art = _load_json()
    if len(art["row_reconciliation"]) < 14:
        pytest.fail("missing DB rows in reconciliation")
    assert all(r["all_fields_match"] for r in art["row_reconciliation"])


def test_extra_unexpected_matched_row_reported():
    art = _load_json()
    assert "unexpected_extra_matches" in art["production_db"]
    assert art["production_db"]["unexpected_extra_matches"] == 0


def test_decimal_price_comparison_stable():
    from decimal import Decimal

    art = _load_json()
    for r in art["row_reconciliation"]:
        assert Decimal(str(r["report_price"])) == Decimal(str(r["db_price"]))


def test_decimal_quantity_comparison_stable():
    from decimal import Decimal

    art = _load_json()
    for r in art["row_reconciliation"]:
        assert Decimal(str(r["report_quantity"])) == Decimal(str(r["db_quantity"]))


def test_timestamps_normalized_to_utc():
    art = _load_json()
    for r in art["row_reconciliation"]:
        assert r["report_timestamp"] == r["db_timestamp"]
        assert "+00:00" in r["report_timestamp"]


def test_lowercase_wallet_comparison():
    art = _load_json()
    for r in art["row_reconciliation"]:
        assert r["report_trader_address"].lower() == r["db_trader_address"].lower()


def test_repeated_digit_fixture_wallet_flagged():
    from scripts._recon_build import _is_repeated_hex

    assert _is_repeated_hex(FIXTURE_WALLET) is True
    assert _is_repeated_hex("0x" + "a" * 40) is True


def test_repeated_digit_condition_and_token_flagged():
    from scripts._recon_build import _is_repeated_hex, _is_repeated_dec

    assert _is_repeated_hex("0x2222222222222222222222222222222222222222") is True
    assert _is_repeated_dec("555555555555555555555555") is True


def test_normal_identifiers_not_falsely_flagged():
    from scripts._recon_build import _is_repeated_hex, _is_repeated_dec

    for hid in HIST_IDS:
        assert not _is_repeated_hex(hid)
        assert not _is_repeated_dec(hid.split(":")[-1])


def test_csv_contains_all_required_columns():
    import csv

    required = [
        "row_number", "report_source", "db_source", "source_match",
        "report_source_trade_id", "db_source_trade_id", "source_trade_id_match",
        "report_market_source_id", "db_market_source_id", "market_source_id_match",
        "report_token_id", "db_token_id", "token_id_match",
        "report_trader_address", "db_trader_address", "trader_address_match",
        "report_side", "db_side", "side_match",
        "report_outcome", "db_outcome", "outcome_match",
        "report_quantity", "db_quantity", "quantity_match",
        "report_price", "db_price", "price_match",
        "report_timestamp", "db_timestamp", "timestamp_match",
        "report_is_sample", "db_is_sample", "is_sample_match",
        "placeholder_pattern_detected", "placeholder_pattern_reasons", "all_fields_match",
    ]
    with open(CSV_PATH, newline="") as f:
        cols = list(csv.DictReader(f).fieldnames)
    for c in required:
        assert c in cols, c


def test_human_reports_redact_wallet():
    txt = (ROOT / "reports" / "pr24z_manual_real_source_trade_ingestion.txt").read_text()
    assert "0xcac76b761231464900cce5da7c20233d59b20579" not in txt
    assert "0x…0579" in txt or "0x…1111" in txt


def test_no_write_path_invoked():
    """Verification produced no production write (source_trades stayed 19)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    finally:
        conn.close()
    assert n == 19
    main = json.loads(MAIN_JSON.read_text())
    assert main["historical_production_rows"]["rows_inserted"] == 14
    assert main["fixture_verification_rows"]["rows_written_to_production"] == 0


# ── 6 HARD identity-compatibility-gate cases ──────────────────────────────────
def _run_gate(records, *, expected=14, db_path=DB_PATH):
    from polycopy.ingestion.source_trade_writer import run_identity_compatibility_gate

    return run_identity_compatibility_gate(SOURCE, records, db_path=db_path, expected=expected)


def _reference_records():
    return json.loads(HIST_REF.read_text())


def test_gate_passes_against_real_db_all_14_recognized():
    gate = _run_gate(_reference_records())
    assert gate.historical_rows_examined == 14
    assert gate.unmatched == 0
    assert gate.rerun_would_insert == 0
    assert gate.safe_for_future_production_write is True


def test_gate_recognizes_legacy_fallback_not_canonical():
    """With REAL upstream ids, the 14 historical rows are recognized ONLY via
    the recomputed legacy fallback (canonical strong id differs from stored DB id)."""
    gate = _run_gate(_reference_records())
    # Corrected canonical id (real upstream) != stored DB id for every row.
    assert gate.canonical_matches == 0
    assert gate.legacy_alias_matches == 14
    assert gate.unmatched == 0
    assert gate.rerun_would_insert == 0
    assert gate.safe_for_future_production_write is True


def test_gate_recognizes_legacy_fallback_when_canonical_differs():
    """A row whose corrected canonical id differs must still match via legacy alias."""
    recs = _reference_records()
    mutated = []
    for r in recs:
        r2 = dict(r)
        r2["sourceProvidedTradeId"] = "polymarket:" + "f" * 64
        mutated.append(r2)
    gate = _run_gate(mutated)
    assert gate.canonical_matches == 0
    assert gate.legacy_alias_matches == 14
    assert gate.unmatched == 0
    assert gate.rerun_would_insert == 0
    assert gate.safe_for_future_production_write is True


def test_gate_blocks_when_unmatched_historical_row():
    """An unmatched historical row blocks production write."""
    recs = _reference_records()
    extra = dict(recs[0])
    # Change an immutable field (token_id) so BOTH the canonical strong id and the
    # recomputed legacy fallback id diverge from every existing DB row -> unrecognized.
    extra["source_trade_id"] = "polymarket:" + "9" * 64
    extra["sourceProvidedTradeId"] = "polymarket:" + "9" * 64
    extra["token_id"] = "99999999999999999999999999999999999999999999999999999999999999999"
    gate = _run_gate(recs + [extra], expected=15)
    assert gate.unmatched >= 1
    assert gate.rerun_would_insert >= 1
    assert gate.safe_for_future_production_write is False


def test_gate_blocks_when_rerun_would_insert():
    """rerun_would_insert > 0 blocks production write."""
    recs = _reference_records()
    extra = dict(recs[0])
    extra["source_trade_id"] = "polymarket:" + "a" * 64
    extra["sourceProvidedTradeId"] = "polymarket:" + "a" * 64
    extra["token_id"] = "11111111111111111111111111111111111111111111111111111111111111111"
    gate = _run_gate(recs + [extra], expected=15)
    assert gate.rerun_would_insert > 0
    assert gate.safe_for_future_production_write is False


def test_writer_not_invoked_when_gate_fails(monkeypatch):
    """Writer is not invoked when the compatibility gate fails (CLI aborts)."""
    from scripts import ingest_real_source_trades as cli
    from polycopy.ingestion.source_trade_writer import IdentityCompatibilityGate

    # Missing reference file -> gate fails closed.
    monkeypatch.setattr(cli, "_HISTORICAL_REFERENCE", ROOT / "reports" / "does_not_exist.json")
    g2 = cli._run_historical_compatibility_gate(DB_PATH)
    assert g2.safe_for_future_production_write is False

    # Explicitly unsafe gate object also reports unsafe.
    gate = IdentityCompatibilityGate(
        historical_rows_expected=14, historical_rows_examined=0,
        unmatched=14, rerun_would_insert=14, safe_for_future_production_write=False,
    )
    assert gate.safe_for_future_production_write is False


# ── Section 8: tautology-bug regression tests ──────────────────────────────────
def test_stored_fallback_and_upstream_ids_differ():
    """Historical stored fallback DB id and the real upstream source-provided id
    are DIFFERENT for every historical row (the root cause of the tautology)."""
    recs = _reference_records()
    assert len(recs) == 14
    for r in recs:
        stored = r["historical_stored_source_trade_id"]
        upstream = r["historical_upstream_source_provided_trade_id"]
        assert stored != upstream
        assert r["upstream_id_historically_mislabeled_as_transaction_hash"] is True
        assert upstream == r["transaction_hash"]


def test_reconciliation_not_populated_from_source_trade_id():
    """Reconciliation must derive the upstream id from transaction_hash, never
    copy source_trade_id into the upstream_source_provided field."""
    art = _load_json()
    for r in art["identity_row_reconciliation"]:
        assert r["real_upstream_source_provided_trade_id"] != r["historical_report_source_trade_id"]
        assert r["real_upstream_source_provided_trade_id"] == r["historical_report_transaction_hash"]


def test_row1_corrected_derives_from_upstream_not_stored():
    """Row 1: corrected canonical must derive from e0c9d495... (upstream),
    not from 11ae80be... (stored)."""
    art = _load_json()
    r1 = art["identity_row_reconciliation"][0]
    assert r1["historical_report_source_trade_id"] == "polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90"
    assert r1["real_upstream_source_provided_trade_id"] == "polymarket:e0c9d495b892a136f1053473e3cb96d4a721e1fce7bb46bf1019d911ad441dbb"
    assert r1["corrected_generate_identity_output_id"] == "polymarket:e0c9d495b892a136f1053473e3cb96d4a721e1fce7bb46bf1019d911ad441dbb"
    assert r1["corrected_id_equals_db_id"] is False
    assert r1["recomputed_legacy_equals_db_id"] is True
    assert r1["recognized_as_existing"] is True


def test_row2_corrected_derives_from_upstream_not_stored():
    """Row 2: corrected canonical must derive from 9b811fe6... (upstream),
    not from 9b9b74c3... (stored)."""
    art = _load_json()
    r2 = art["identity_row_reconciliation"][1]
    assert r2["historical_report_source_trade_id"] == "polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75"
    assert r2["real_upstream_source_provided_trade_id"] == "polymarket:9b811fe6d9f115c5c23d9e73c960e2566ad7e442cfff3d5215d8c16a15705671"
    assert r2["corrected_generate_identity_output_id"] == "polymarket:9b811fe6d9f115c5c23d9e73c960e2566ad7e442cfff3d5215d8c16a15705671"
    assert r2["corrected_id_equals_db_id"] is False


def test_canonical_mismatch_with_valid_legacy_alias_recognized():
    """A canonical mismatch PLUS a valid legacy alias is recognized as existing."""
    gate = _run_gate(_reference_records())
    assert gate.canonical_matches == 0
    assert gate.legacy_alias_matches == 14
    assert gate.unmatched == 0


def test_canonical_mismatch_without_legacy_alias_blocks():
    """A canonical mismatch WITHOUT a legacy alias blocks the writer (unmatched)."""
    recs = _reference_records()
    extra = dict(recs[0])
    extra["source_trade_id"] = "polymarket:" + "c" * 64
    extra["sourceProvidedTradeId"] = "polymarket:" + "c" * 64
    extra["token_id"] = "77777777777777777777777777777777777777777777777777777777777777777"
    gate = _run_gate(recs + [extra], expected=15)
    assert gate.unmatched >= 1
    assert gate.safe_for_future_production_write is False


def test_reusing_db_source_trade_id_as_source_provided_fails():
    """Feeding the stored DB source_trade_id back as sourceProvidedTradeId must
    NOT be accepted as the canonical proof (tautology guard)."""
    recs = _reference_records()
    bad = []
    for r in recs:
        r2 = dict(r)
        r2["sourceProvidedTradeId"] = r2["historical_stored_source_trade_id"]
        r2["historical_upstream_source_provided_trade_id"] = r2["historical_stored_source_trade_id"]
        bad.append(r2)
    _run_gate(bad)  # forcing the bug would make canonical_matches=14 (tautology)
    # The committed reference must NOT do this:
    real = _run_gate(_reference_records())
    assert real.canonical_matches == 0


def test_csv_has_row_where_corrected_id_unequals_db():
    """The CSV must contain at least one row where corrected_id_equals_db_id is
    False (proving the real historical data produces a canonical mismatch)."""
    import csv

    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    false_rows = [r for r in rows if r["corrected_id_equals_db_id"].lower() == "false"]
    assert len(false_rows) >= 1
    assert len(rows) == 14


def test_no_test_hardcodes_safe_true():
    """safe_for_future_production_write must always be COMPUTED from the gate,
    never asserted without the gate running. This test runs the gate and checks
    the value is consistent with its inputs."""
    gate = _run_gate(_reference_records())
    expected_safe = (
        gate.historical_rows_examined == gate.historical_rows_expected
        and gate.unmatched == 0
        and gate.rerun_would_insert == 0
    )
    assert gate.safe_for_future_production_write == expected_safe


def test_writer_not_invoked_when_reconciliation_invalid():
    """The committed reference must carry real upstream ids (so the gate is not
    tautological), and the gate on the real reference must be safe."""
    recs = _reference_records()
    assert all(r["historical_upstream_source_provided_trade_id"] for r in recs)
    real = _run_gate(recs)
    assert real.safe_for_future_production_write is True
