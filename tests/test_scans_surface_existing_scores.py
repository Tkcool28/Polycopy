"""Tests for /scans endpoint surfacing existing wallet_score_decisions.

These tests prove the wiring gap fix in DashboardRepository.scans():
wallets with a real wallet_score_decisions row now expose their final_score
and verdict via the /scans endpoint, while wallets without any decision
remain in their previous INCOMPLETE state. Read-only — no DB writes.

This is a hotfix branch (fix/scans-surface-existing-scores) that does NOT
touch scoring formulas, scoring pipelines, paper trading settings, or the
kill switch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from polycopy.api.app import app
from polycopy.config.settings import get_settings
from polycopy.db import database as db_module
from polycopy.db.database import get_database

WALLET_SCORED_ID = "11111111-1111-1111-1111-111111111111"
WALLET_SCORED_TWICE_ID = "22222222-2222-2222-2222-222222222222"
WALLET_UNSCORED_ID = "33333333-3333-3333-3333-333333333333"
FORMULA_NAME = "copyability_default"
FORMULA_VERSION = "v1"


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
    get_settings(reload=True)
    get_database(reload=True)
    yield TestClient(app)
    if db_module._db is not None:
        db_module._db.close()
        db_module._db = None
    get_settings(reload=True)


def _seed_wallets() -> None:
    db = get_database()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    rows = [
        (WALLET_SCORED_ID, "0xAAA", "scored-once", 0),
        (WALLET_SCORED_TWICE_ID, "0xBBB", "scored-twice", 0),
        (WALLET_UNSCORED_ID, "0xCCC", "unscored", 0),
    ]
    for wid, address, label, is_sample in rows:
        db.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (wid, address, label, is_sample, now),
        )


def _seed_score_decision(
    wallet_id: str,
    final_score: float,
    verdict: str,
    computed_at: str,
) -> None:
    """Insert one wallet_score_decisions row with the required unique key."""
    db = get_database()
    db.execute(
        """
        INSERT INTO wallet_score_decisions (
            wallet_id, formula_name, formula_version, idempotency_key,
            final_score, verdict, computed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            wallet_id,
            FORMULA_NAME,
            FORMULA_VERSION,
            f"idemp-{wallet_id}-{computed_at}",
            final_score,
            verdict,
            computed_at,
            computed_at,
        ),
    )


class TestScansSurfacesExistingScores:
    """End-to-end /scans behaviour with seeded wallet_score_decisions."""

    def test_scored_wallet_exposes_real_score_and_verdict(self, api_client: TestClient):
        _seed_wallets()
        scored_at = "2026-07-04T10:00:00+00:00"
        _seed_score_decision(
            wallet_id=WALLET_SCORED_ID,
            final_score=42.5,
            verdict="skip",
            computed_at=scored_at,
        )

        resp = api_client.get("/scans", params={"limit": 50})
        assert resp.status_code == 200
        data = resp.json()
        scored = next(
            (s for s in data["scans"] if s["address"] == "0xAAA"),
            None,
        )
        assert scored is not None, "scored wallet must appear in /scans"
        assert scored["score"] == 42.5
        assert scored["verdict"] == "skip"
        # source_count semantics unchanged (joins performance_summaries)
        assert scored["source_count"] == 0

    def test_unscored_wallet_remains_incomplete(self, api_client: TestClient):
        _seed_wallets()

        resp = api_client.get("/scans", params={"limit": 50})
        assert resp.status_code == 200
        data = resp.json()
        unscored = next(
            (s for s in data["scans"] if s["address"] == "0xCCC"),
            None,
        )
        assert unscored is not None, "unscored wallet must appear in /scans"
        assert unscored["score"] is None
        assert unscored["verdict"] == "INCOMPLETE"
        assert unscored["source_count"] == 0

    def test_only_latest_decision_is_used_for_score(self, api_client: TestClient):
        """If a wallet has multiple wallet_score_decisions rows, the most
        recent by computed_at wins. Older decisions must not leak through.
        """
        _seed_wallets()
        older = "2026-07-01T10:00:00+00:00"
        newer = "2026-07-04T10:00:00+00:00"
        _seed_score_decision(
            wallet_id=WALLET_SCORED_TWICE_ID,
            final_score=5.0,
            verdict="skip",
            computed_at=older,
        )
        _seed_score_decision(
            wallet_id=WALLET_SCORED_TWICE_ID,
            final_score=88.0,
            verdict="watchlist",
            computed_at=newer,
        )

        resp = api_client.get("/scans", params={"limit": 50})
        assert resp.status_code == 200
        data = resp.json()
        row = next(s for s in data["scans"] if s["address"] == "0xBBB")
        assert row["score"] == 88.0, "newer decision must win, not 5.0"
        assert row["verdict"] == "watchlist"

    def test_empty_db_returns_empty_list_no_scores(self, api_client: TestClient):
        """Without any wallets or decisions, /scans returns an empty list
        and is_sample_data=False (demo mode is off in this fixture)."""
        resp = api_client.get("/scans")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scans"] == []
        assert data["total_count"] == 0
        assert data["is_sample_data"] is False

    def test_pagination_preserves_score_wiring(self, api_client: TestClient):
        """Ensure the LEFT JOIN does not break limit/offset semantics."""
        _seed_wallets()
        _seed_score_decision(
            wallet_id=WALLET_SCORED_ID,
            final_score=10.0,
            verdict="skip",
            computed_at="2026-07-04T11:00:00+00:00",
        )

        resp = api_client.get("/scans", params={"limit": 1, "offset": 0})
        assert resp.status_code == 200
        page = resp.json()
        assert page["total_count"] >= 3
        assert len(page["scans"]) == 1

    # ── Ordering tests (scored-first, score DESC, computed_at tiebreak) ──────

    def test_scored_wallets_sort_before_unscored(self, api_client: TestClient):
        """Scored wallets must appear on page 0 before unscored ones."""
        _seed_wallets()
        # Score the wallet that sorts LAST by created_at among our 3 seeds,
        # so the OLD ordering would hide it; the new ordering must surface it.
        _seed_score_decision(
            wallet_id=WALLET_UNSCORED_ID,
            final_score=12.345,
            verdict="skip",
            computed_at="2026-07-04T11:00:00+00:00",
        )

        resp = api_client.get("/scans", params={"limit": 50})
        assert resp.status_code == 200
        rows = resp.json()["scans"]
        scored_addresses = {s["address"] for s in rows if s["score"] is not None}
        # WALLET_UNSCORED_ID is the one we scored; its address is 0xCCC.
        assert "0xCCC" in scored_addresses
        # And it must be the FIRST row of the page (scored-first ordering).
        assert rows[0]["address"] == "0xCCC"
        assert rows[0]["score"] == 12.345
        assert rows[0]["verdict"] == "skip"

    def test_higher_score_sorts_before_lower_score(self, api_client: TestClient):
        """Among scored wallets, highest final_score sorts first."""
        _seed_wallets()
        # Score two wallets. LOW is created FIRST, HIGH second.
        # Under any created_at ordering, HIGH would come after LOW.
        # Under the new ordering, HIGH must come BEFORE LOW because 88 > 10.
        _seed_score_decision(
            wallet_id=WALLET_SCORED_ID,
            final_score=10.0,
            verdict="skip",
            computed_at="2026-07-04T10:00:00+00:00",
        )
        _seed_score_decision(
            wallet_id=WALLET_SCORED_TWICE_ID,
            final_score=88.0,
            verdict="watchlist",
            computed_at="2026-07-04T10:00:00+00:00",
        )

        resp = api_client.get("/scans", params={"limit": 50})
        rows = resp.json()["scans"]
        scored_rows = [r for r in rows if r["score"] is not None]
        assert len(scored_rows) == 2
        # Highest score must come first.
        assert scored_rows[0]["score"] == 88.0
        assert scored_rows[0]["address"] == "0xBBB"
        assert scored_rows[1]["score"] == 10.0
        assert scored_rows[1]["address"] == "0xAAA"

    def test_newer_computed_at_breaks_score_ties(self, api_client: TestClient):
        """When two wallets have the same final_score, the one with the
        more recent computed_at sorts first."""
        _seed_wallets()
        older = "2026-07-01T10:00:00+00:00"
        newer = "2026-07-04T10:00:00+00:00"
        # Same score, different computed_at — newer must win.
        _seed_score_decision(
            wallet_id=WALLET_SCORED_ID,
            final_score=50.0,
            verdict="skip",
            computed_at=older,
        )
        _seed_score_decision(
            wallet_id=WALLET_SCORED_TWICE_ID,
            final_score=50.0,
            verdict="skip",
            computed_at=newer,
        )

        resp = api_client.get("/scans", params={"limit": 50})
        rows = resp.json()["scans"]
        scored_rows = [r for r in rows if r["score"] is not None]
        assert len(scored_rows) == 2
        assert scored_rows[0]["score"] == 50.0
        # The newer one (WALLET_SCORED_TWICE_ID, 0xBBB) must come first.
        assert scored_rows[0]["address"] == "0xBBB"
        assert scored_rows[1]["address"] == "0xAAA"

    def test_unscored_wallets_return_incomplete_in_new_ordering(self, api_client: TestClient):
        """Unscored wallets still report score=null, verdict=INCOMPLETE
        even after the scoring-first reordering."""
        _seed_wallets()
        _seed_score_decision(
            wallet_id=WALLET_SCORED_ID,
            final_score=42.5,
            verdict="skip",
            computed_at="2026-07-04T10:00:00+00:00",
        )

        resp = api_client.get("/scans", params={"limit": 50})
        rows = resp.json()["scans"]
        unscored = next(r for r in rows if r["address"] == "0xCCC")
        assert unscored["score"] is None
        assert unscored["verdict"] == "INCOMPLETE"
        # And the unscored wallet sits AFTER the scored ones in the page.
        scored_addresses = {r["address"] for r in rows if r["score"] is not None}
        assert "0xAAA" in scored_addresses
        assert unscored["address"] not in scored_addresses

    def test_empty_db_returns_empty_after_reorder(self, api_client: TestClient):
        """Reordering must not break the empty-DB path."""
        resp = api_client.get("/scans")
        assert resp.status_code == 200
        data = resp.json()
        assert data["scans"] == []
        assert data["total_count"] == 0

    def test_pagination_after_reorder_returns_correct_pages(self, api_client: TestClient):
        """The new ORDER BY is deterministic, so limit/offset walks the same
        ranking. limit=2 offset=0 returns the top 2 scored wallets; offset=2
        returns the next scored wallet followed by unscored ones."""
        _seed_wallets()
        _seed_score_decision(
            wallet_id=WALLET_SCORED_ID,
            final_score=10.0,
            verdict="skip",
            computed_at="2026-07-04T10:00:00+00:00",
        )
        _seed_score_decision(
            wallet_id=WALLET_SCORED_TWICE_ID,
            final_score=88.0,
            verdict="watchlist",
            computed_at="2026-07-04T10:00:00+00:00",
        )

        page1 = api_client.get("/scans", params={"limit": 2, "offset": 0}).json()
        page2 = api_client.get("/scans", params={"limit": 2, "offset": 2}).json()
        # Page 1: top 2 scored (88.0, then 10.0).
        assert len(page1["scans"]) == 2
        assert page1["scans"][0]["score"] == 88.0
        assert page1["scans"][1]["score"] == 10.0
        # Page 2: the 2 unscored wallets (WALLET_UNSCORED_ID is 0xCCC).
        assert len(page2["scans"]) == 1
        assert page2["scans"][0]["score"] is None
        assert page2["scans"][0]["address"] == "0xCCC"
        # total_count remains 3 across pages.
        assert page1["total_count"] == 3
        assert page2["total_count"] == 3