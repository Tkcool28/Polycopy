"""Research-only specialist evidence watchlist service.

Durable membership granting RESEARCH permission only. One ACTIVE watch per
wallet (enforced by the partial unique index ux_evidence_watchlist_active).
This module NEVER creates a specialist_approval, dispatch, candidate, or any
execution-plane row, and exposes no relationship that execution code could
treat as an authorization.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from polycopy.db.database import Database

WATCHLIST_SOURCE_MANUAL = "manual"
WATCHLIST_SOURCE_DISCOVERY = "discovery"
WATCHLIST_STATUSES = ("active", "paused", "retired")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _uuid() -> str:
    import uuid

    return f"wl_{uuid.uuid4().hex}"


def _wallet_is_sample(db: Database, wallet_id: str) -> bool:
    row = db.fetchone("SELECT is_sample FROM wallets WHERE id=?", (wallet_id,))
    if row is None:
        return False
    return bool(dict(row).get("is_sample"))


def active_watch_for_wallet(db: Database, wallet_id: str) -> Optional[str]:
    row = db.fetchone(
        "SELECT id FROM specialist_evidence_watchlist "
        "WHERE wallet_id=? AND status='active'",
        (wallet_id,),
    )
    return dict(row)["id"] if row is not None else None


def add_watch(
    db: Database,
    *,
    wallet_id: str,
    source: str = WATCHLIST_SOURCE_MANUAL,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
    max_new_trades_per_run: int = 25,
) -> str:
    """Create an ACTIVE watch for a non-sample wallet. Idempotent: if an
    active watch already exists, return its id without creating a duplicate."""
    if source not in (WATCHLIST_SOURCE_MANUAL, WATCHLIST_SOURCE_DISCOVERY):
        raise ValueError(f"invalid source: {source}")
    if _wallet_is_sample(db, wallet_id):
        raise ValueError("sample wallet rejected")
    existing = active_watch_for_wallet(db, wallet_id)
    if existing is not None:
        return existing  # one active per wallet
    wid = _uuid()
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist("
        "id, wallet_id, status, source, reason, created_by, created_at,"
        "max_new_trades_per_run) VALUES (?,?, 'active', ?,?,?,?,?)",
        (wid, wallet_id, source, reason, created_by, _now_iso(),
         int(max_new_trades_per_run)),
    )
    db.conn.commit()
    return wid


def _set_status(db: Database, watch_id: str, status: str) -> bool:
    cur = db.conn.execute(
        "UPDATE specialist_evidence_watchlist SET status=?, "
        "paused_at=?, retired_at=? WHERE id=?",
        (status,
         _now_iso() if status == "paused" else None,
         _now_iso() if status == "retired" else None,
         watch_id),
    )
    db.conn.commit()
    return cur.rowcount == 1


def pause_watch(db: Database, watch_id: str) -> bool:
    return _set_status(db, watch_id, "paused")


def resume_watch(db: Database, watch_id: str) -> bool:
    # Resume only if currently paused (retired stays retired).
    row = db.fetchone(
        "SELECT status FROM specialist_evidence_watchlist WHERE id=?", (watch_id,)
    )
    if row is None or dict(row)["status"] != "paused":
        return False
    return _set_status(db, watch_id, "active")


def retire_watch(db: Database, watch_id: str) -> bool:
    return _set_status(db, watch_id, "retired")


def list_watches(db: Database, *, status: Optional[str] = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, wallet_id, status, source, reason, created_by, created_at, "
        "paused_at, retired_at, max_new_trades_per_run, last_collection_at "
        "FROM specialist_evidence_watchlist"
    )
    params: list[Any] = []
    if status is not None:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY wallet_id, id"
    rows = db.conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def inspect_watch(db: Database, watch_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT id, wallet_id, status, source, reason, created_by, created_at, "
        "paused_at, retired_at, max_new_trades_per_run, last_collection_at "
        "FROM specialist_evidence_watchlist WHERE id=?",
        (watch_id,),
    )
    return dict(row) if row is not None else None
