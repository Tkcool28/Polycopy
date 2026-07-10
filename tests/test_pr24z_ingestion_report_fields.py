"""PR24Z ingestion report-field regression tests (post-migration semantics).

These tests verify the corrected report generation:
- compatibility block reports genuine-new rows, not "unmatched historical rows"
- canonical_migration block reflects runtime marker validation (never "pending")
- production-write vs dry-run reports show distinct write intent/commit status

No production DB access; all inputs are in-memory fixtures.
"""
from __future__ import annotations

from types import SimpleNamespace

from scripts.ingest_real_source_trades import (
    _build_canonical_migration_block,
    _render_markdown,
    _render_txt,
)


def _fake_marker(*, valid: bool, legacy: int = 0, canonical: int = 14, reasons=()):
    return SimpleNamespace(valid=valid, data={"legacy_row_count": legacy, "canonical_row_count": canonical}, reasons=tuple(reasons))


def _fake_write(*, committed: bool):
    return SimpleNamespace(attempted=12, inserted=4, deduplicated=8, rejected=0, errors=0, committed=committed, rolled_back=not committed, existing_duplicates_recognized=8, unique_constraint_present=True, error_message=None)


def _compatibility_dict(matched: int, total_valid: int) -> dict:
    """Mirror the exact computation the generator performs (post-migration)."""
    return {
        "historical_migrated_rows_total": 14,
        "migrated_rows_present_in_current_fetch": matched,
        "migrated_rows_matched_canonically": matched,
        "migrated_rows_failed_to_match": 0,
        "new_canonical_rows_in_current_fetch": total_valid - matched,
        "legacy_identity_aliases_used": 0,
        "dry_run_would_insert_new_rows": total_valid - matched,
        "reconciliation_error": None,
    }


def _base_payload(mode, *, marker, compatibility, write=None, write_req=False, write_commit=False):
    cm = _build_canonical_migration_block(marker, production_write_requested=write_req, production_write_committed=write_commit)
    return {
        "mode": mode,
        "generated_at": "2026-07-10T00:00:00+00:00",
        "ingestion_version": "PR24Z-1",
        "source": "polymarket_data_api_trades_user",
        "wallet_address": "0x" + "1" * 40,
        "wallet_address_redacted": "0x…0579",
        "live": True,
        "network_calls_attempted": 2,
        "network_calls_succeeded": 2,
        "counts": {"raw_records": 25, "raw_buy_records": 12, "raw_sell_records": 13, "unknown_side_records": 0,
                   "eligible_buy_records": 12, "rejected_unsupported_side": 13, "rejected_missing_fields": 0,
                   "rejected_invalid_price": 0, "rejected_invalid_quantity": 0, "rejected_invalid_timestamp": 0,
                   "rejected_wallet_mismatch": 0, "rejected_invalid_fields": 0, "rows_rejected": 13},
        "identity": {"source_provided_identity_used_count": 25, "transaction_identity_used_count": 0,
                     "strong_identity_used_count": 25, "identity_fallback_used_count": 0, "identity_ambiguous_count": 0,
                     "duplicate_records_in_fetch": 0, "duplicate_records_existing_db": 8, "collision_errors": 0},
        "readiness": {"pr24u_ready_count": 25, "pr24v_ready_count": 25, "both_ready_count": 25,
                      "ready_for_scoring": False, "ready_for_automation": False},
        "safety": {"downstream_tables_changed": False, "timers_changed": False},
        "canonical_migration": cm,
        "compatibility": compatibility,
        "write": (write.__dict__ if write is not None else None),
        "backup": {"method": "sqlite_online_backup", "path": "/tmp/x.backup", "sha256": "0" * 64, "size": 528384,
                   "integrity_check": "ok", "foreign_key_violations": 0, "source_trades_count": 19,
                   "success": True, "error": None},
        "unique_constraint": None,
        "process_gate": None,
        "historical_first_production_write": {"attempted": 14, "inserted": 14, "deduplicated": 0},
        "db_before": {"size": 528384, "mtime": 0, "path": "/tmp/x", "counts": {"source_trades": 19}},
        "db_after": {"size": 528384, "mtime": 0, "path": "/tmp/x", "counts": {"source_trades": 19}},
        "db_path": "/tmp/x",
        "integrity_check": "ok",
        "foreign_key_check": 0,
    }


# ── canonical_migration block ───────────────────────────────────────────────
def test_build_canonical_migration_block_valid_marker():
    cm = _build_canonical_migration_block(_fake_marker(valid=True))
    assert cm["migration_complete"] is True
    assert cm["marker_validated"] is True
    assert cm["production_write_authorized_by_marker"] is True
    assert cm["legacy_rows_remaining"] == 0
    assert cm["migrated_canonical_rows_present"] == 14
    assert cm["legacy_identity_aliases_used"] == 0


def test_build_canonical_migration_block_invalid_marker():
    cm = _build_canonical_migration_block(_fake_marker(valid=False, reasons=("missing marker",)))
    assert cm["migration_complete"] is False
    assert cm["marker_validated"] is False
    assert cm["production_write_authorized_by_marker"] is False
    assert cm["marker_validation_errors"] == ["missing marker"]


# ── compatibility block semantics ───────────────────────────────────────────
def test_compatibility_block_reports_new_rows_not_unmatched_historical():
    comp = _compatibility_dict(matched=8, total_valid=12)
    assert comp["historical_migrated_rows_total"] == 14
    assert comp["migrated_rows_present_in_current_fetch"] == 8
    assert comp["migrated_rows_matched_canonically"] == 8
    assert comp["migrated_rows_failed_to_match"] == 0
    assert comp["new_canonical_rows_in_current_fetch"] == 4
    assert "existing_pr24z_rows_unmatched" not in comp
    assert "rerun_would_insert" not in comp


# ── dry-run vs production-write write intent/commit flags ───────────────────
def test_dry_run_report_shows_false_write_flags():
    p = _base_payload("safety-verification", marker=_fake_marker(valid=True),
                      compatibility=_compatibility_dict(8, 12), write_req=False, write_commit=False)
    assert p["canonical_migration"]["production_write_requested"] is False
    assert p["canonical_migration"]["production_write_committed"] is False


def test_production_write_report_shows_true_write_flags():
    p = _base_payload("production-write", marker=_fake_marker(valid=True),
                      compatibility=None, write=_fake_write(committed=True), write_req=True, write_commit=True)
    assert p["canonical_migration"]["production_write_requested"] is True
    assert p["canonical_migration"]["production_write_committed"] is True


# ── rendered output must not contain stale "pending" claims ─────────────────
def test_render_markdown_no_stale_pending_claims():
    p = _base_payload("production-write", marker=_fake_marker(valid=True),
                      compatibility=_compatibility_dict(8, 12), write=_fake_write(committed=True),
                      write_req=True, write_commit=True)
    md = _render_markdown(p)
    assert "future_production_write_allowed_now" not in md
    assert "temporary_write_block" not in md
    assert "canonical_migration_required_before_next_production_write" not in md
    assert "migration_complete: True" in md
    assert "migrated_rows_present_in_current_fetch: 8" in md
    assert "migrated_rows_failed_to_match: 0" in md
    assert "new_canonical_rows_in_current_fetch: 4" in md


def test_render_txt_no_stale_pending_claims():
    p = _base_payload("production-write", marker=_fake_marker(valid=True),
                      compatibility=_compatibility_dict(8, 12), write=_fake_write(committed=True),
                      write_req=True, write_commit=True)
    txt = _render_txt(p)
    assert "future_production_write_allowed_now=False" not in txt
    assert "temporary_write_block=missing" not in txt
    assert "migration_complete=True" in txt


def test_render_markdown_dry_run_shows_false_write_flags():
    p = _base_payload("safety-verification", marker=_fake_marker(valid=True),
                      compatibility=_compatibility_dict(8, 12), write_req=False, write_commit=False)
    md = _render_markdown(p)
    assert "production_write_requested: False" in md
    assert "production_write_committed: False" in md


def test_render_markdown_production_write_shows_true_write_flags():
    p = _base_payload("production-write", marker=_fake_marker(valid=True),
                      compatibility=None, write=_fake_write(committed=True), write_req=True, write_commit=True)
    md = _render_markdown(p)
    assert "production_write_requested: True" in md
    assert "production_write_committed: True" in md
