from __future__ import annotations

import json
from pathlib import Path

from polycopy.db.database import Database
from polycopy.scoring.wallet_evidence import (
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_UNAVAILABLE,
    CATEGORY_TAXONOMY_USABLE,
    aggregate_category_evidence,
    aggregate_wallet_evidence,
    classify_category_taxonomy,
)

WALLET = "0x" + "a" * 40


def _db(tmp_path: Path):
    db = Database(tmp_path / "evidence.db").connect()
    db.execute("INSERT INTO wallets (id, address, canonical_address, created_at) VALUES ('w1', ?, ?, '2026-01-01T00:00:00Z')", (WALLET, WALLET))
    db.conn.commit()
    return db


def _trade(db, ident: str, *, side="BUY", timestamp="2026-01-01T00:00:00Z", status="won", winning=1, pnl=1.0, market="m1", metadata=None):
    db.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id, resolution_status, is_winning_trade, realized_pnl, metadata_json) VALUES (?, 'polymarket', ?, ?, ?, 'Yes', 2, .5, ?, ?, 0, ?, ?, ?, ?, ?)",
        (ident, ident, market, side, WALLET, timestamp, ident + '-token', status, winning, pnl, json.dumps(metadata or {})),
    )
    db.conn.commit()


def _meta(*, event_id="e1", slug="event-1", category="Politics", title="ignored title", tags=()):
    return {"event": {"id": event_id, "slug": slug, "title": title}, "taxonomy": {"raw_category": category, "tags": list(tags)}}


def test_wallet_evidence_buy_resolution_sell_and_events(tmp_path):
    db = _db(tmp_path)
    _trade(db, "winner", pnl=1.0, metadata=_meta(event_id="e1", category="Politics"))
    _trade(db, "loser", timestamp="2026-01-02T00:00:00Z", status="lost", winning=0, pnl=-1.0, market="m2", metadata=_meta(event_id=None, slug="event-2", category="Politics"))
    _trade(db, "pending", timestamp="2026-01-03T00:00:00Z", status="unresolved", winning=None, pnl=None, market="m3", metadata=_meta(event_id=None, slug=None, category="Politics"))
    _trade(db, "sell", side="SELL", timestamp="2026-01-04T00:00:00Z", status="won", winning=1, pnl=99.0, market="m4", metadata=_meta(category="Politics"))
    ev = aggregate_wallet_evidence(db, "w1", cutoff_timestamp="2026-01-04T00:00:00Z")
    assert ev.total_buy_trades == 3
    assert ev.resolved_buy_trades == 2
    assert ev.winning_buy_trades == 1 and ev.losing_buy_trades == 1
    assert ev.realized_pnl == 0.0 and ev.win_rate == 0.5
    assert ev.active_trading_days == 3 and ev.distinct_events == 2
    assert ev.unresolved_buy_trades == 1 and ev.missing_event_identity_count == 1
    assert ev.resolved_markets == 2
    db.close()


def test_evidence_fingerprint_stable_changes_and_cutoff(tmp_path):
    db = _db(tmp_path)
    _trade(db, "a", metadata=_meta())
    one = aggregate_wallet_evidence(db, "w1", cutoff_timestamp="2026-01-01T00:00:00Z")
    same = aggregate_wallet_evidence(db, "w1", cutoff_timestamp="2026-01-01T00:00:00Z")
    assert one.evidence_fingerprint == same.evidence_fingerprint
    _trade(db, "later", timestamp="2026-01-02T00:00:00Z", metadata=_meta(event_id="e2"))
    still_one = aggregate_wallet_evidence(db, "w1", cutoff_timestamp="2026-01-01T00:00:00Z")
    changed = aggregate_wallet_evidence(db, "w1", cutoff_timestamp="2026-01-02T00:00:00Z")
    assert still_one.evidence_fingerprint == one.evidence_fingerprint
    assert changed.evidence_fingerprint != one.evidence_fingerprint
    db.close()


def test_taxonomy_never_uses_event_or_title_and_category_filters(tmp_path):
    db = _db(tmp_path)
    usable = classify_category_taxonomy(_meta(category="  US   Politics "))
    assert usable.status == CATEGORY_TAXONOMY_USABLE and usable.category_label == "us politics"
    assert classify_category_taxonomy(_meta(category=None, tags=["politics"])).status == CATEGORY_TAXONOMY_PARTIAL
    assert classify_category_taxonomy({"event": {"slug": "crypto-event", "title": "Crypto"}, "taxonomy": {}}).status == CATEGORY_TAXONOMY_UNAVAILABLE
    _trade(db, "politics", metadata=_meta(category="Politics"))
    _trade(db, "crypto", market="m2", metadata=_meta(event_id="e2", category="Crypto"))
    ev = aggregate_category_evidence(db, "w1", "politics", cutoff_timestamp="2026-01-02T00:00:00Z")
    assert ev.total_buy_trades == 1 and ev.category_label == "politics"
    db.close()
