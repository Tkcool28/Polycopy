"""PR #73 persisted watch-ID compatibility regressions (disposable DB only)."""

from __future__ import annotations

import asyncio
import hashlib
import sys

import pytest

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import collect_specialist_evidence_cohort as cli  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.specialist_evidence_cohort import (  # noqa: E402
    CohortRunConfig,
    CohortValidationError,
    run_cohort,
    validate_watch_ids,
)


DISCOVERY_WATCH_ID = "sew_2eb71e1d1cdf40fa"
LEGACY_WATCH_ID = "wl_deadbeef"


def _open(tmp_path: Path) -> Database:
    return Database(tmp_path / "compatibility.db").connect()


def _seed_active_watch(
    db: Database, *, watch_id: str, wallet_id: str, source: str, is_sample: int = 0
) -> None:
    address = "0x" + hashlib.sha256(wallet_id.encode()).hexdigest()[:40]
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) VALUES (?,?,?,?,?)",
        (wallet_id, address, "test", is_sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.execute(
        """INSERT INTO specialist_evidence_watchlist
           (id,wallet_id,status,source,reason,created_by,created_at,max_new_trades_per_run)
           VALUES (?,?,'active',?,'test','test','2026-01-01T00:00:00Z',25)""",
        (watch_id, wallet_id, source),
    )
    db.conn.commit()


class _NoTradeAdapter:
    async def get_trades_by_address(self, *_args, **_kwargs):
        return []

    def close(self):
        return None


def _assert_all_pr73_validation_layers_accept(db: Database, watch_ids: list[str]) -> None:
    cli._validate_watch_set_shape(watch_ids)
    assert validate_watch_ids(db, watch_ids) == sorted(watch_ids)
    result = asyncio.run(
        run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=_NoTradeAdapter(),
            dry_run=True,
            config=CohortRunConfig(),
        )
    )
    assert result.status == "success"
    assert result.watch_count_processed == len(watch_ids)


def test_legacy_and_discovery_watch_ids_pass_cli_semantic_and_programmatic_validation(tmp_path):
    db = _open(tmp_path)
    try:
        _seed_active_watch(db, watch_id=LEGACY_WATCH_ID, wallet_id="wallet-legacy", source="manual")
        _seed_active_watch(db, watch_id=DISCOVERY_WATCH_ID, wallet_id="wallet-discovery", source="discovery")
        _assert_all_pr73_validation_layers_accept(db, [LEGACY_WATCH_ID])
        _assert_all_pr73_validation_layers_accept(db, [DISCOVERY_WATCH_ID])
    finally:
        db.close()


def test_mixed_prefix_cohort_preserves_existing_semantic_guards(tmp_path):
    db = _open(tmp_path)
    try:
        _seed_active_watch(db, watch_id=LEGACY_WATCH_ID, wallet_id="wallet-legacy", source="manual")
        _seed_active_watch(db, watch_id=DISCOVERY_WATCH_ID, wallet_id="wallet-discovery", source="discovery")
        _assert_all_pr73_validation_layers_accept(db, [DISCOVERY_WATCH_ID, LEGACY_WATCH_ID])
        with pytest.raises(CohortValidationError, match="duplicate watch id"):
            validate_watch_ids(db, [DISCOVERY_WATCH_ID, DISCOVERY_WATCH_ID])
        _seed_active_watch(
            db,
            watch_id="sew_0123456789abcdef",
            wallet_id="wallet-sample",
            source="discovery",
            is_sample=1,
        )
        with pytest.raises(CohortValidationError, match="sample wallet"):
            validate_watch_ids(db, ["sew_0123456789abcdef"])
        db.conn.execute("DROP INDEX ux_evidence_watchlist_active")
        try:
            db.conn.execute(
                """INSERT INTO specialist_evidence_watchlist
                   (id,wallet_id,status,source,reason,created_by,created_at,max_new_trades_per_run)
                   VALUES (?,'wallet-legacy','active','discovery','test','test',
                           '2026-01-01T00:00:00Z',25)""",
                ("sew_fedcba9876543210",),
            )
            db.conn.commit()
            with pytest.raises(CohortValidationError, match="duplicate wallet"):
                validate_watch_ids(db, [LEGACY_WATCH_ID, "sew_fedcba9876543210"])
        finally:
            db.conn.execute("DELETE FROM specialist_evidence_watchlist WHERE id='sew_fedcba9876543210'")
            db.conn.execute(
                "CREATE UNIQUE INDEX ux_evidence_watchlist_active "
                "ON specialist_evidence_watchlist(wallet_id) WHERE status = 'active'"
            )
            db.conn.commit()
        db.conn.execute("UPDATE specialist_evidence_watchlist SET status='paused' WHERE id=?", (DISCOVERY_WATCH_ID,))
        db.conn.commit()
        with pytest.raises(CohortValidationError, match="not active"):
            validate_watch_ids(db, [DISCOVERY_WATCH_ID])
    finally:
        db.close()


@pytest.mark.parametrize(
    "watch_ids",
    [
        ["sew_"],
        ["sew_0123456789abcdeg"],
        ["sew_0123456789abcde"],
        ["sew_0123456789abcdef0"],
        ["other_0123456789abcdef"],
        [DISCOVERY_WATCH_ID, DISCOVERY_WATCH_ID],
    ],
)
def test_malformed_or_duplicate_discovery_ids_fail_before_writable_or_provider(tmp_path, monkeypatch, watch_ids):
    calls = {"writable": 0, "provider": 0}

    def forbidden_writable(*_args, **_kwargs):
        calls["writable"] += 1
        raise AssertionError("invalid discovery IDs must not open writable SQLite")

    class ForbiddenAdapter:
        def __init__(self, **_kwargs):
            calls["provider"] += 1
            raise AssertionError("invalid discovery IDs must not construct a provider")

    monkeypatch.setattr(cli, "open_writable", forbidden_writable)
    import polycopy.adapters.polymarket as polymarket
    monkeypatch.setattr(polymarket, "PolymarketPublicAdapter", ForbiddenAdapter)
    argv = ["--db-path", str(tmp_path / "never-opened.db"), "--write"]
    for watch_id in watch_ids:
        argv.extend(["--watch-id", watch_id])
    assert cli.main(argv) == 2
    assert calls == {"writable": 0, "provider": 0}