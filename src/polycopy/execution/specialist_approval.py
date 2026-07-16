"""Durable manual specialist-wallet approval persistence.

This replaces the single-address ``.env`` implicit approval model with an
explicit, auditable approval record. Discovery and scoring never create
approvals; only the bounded CLI (or an explicit operator action) may.

Canonical ownership: this module is the SOLE writer of ``specialist_approvals``.
The collector and monitor both read the same table (never the .env address as
the authoritative source).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from polycopy.db.wallet_identity import canonical_wallet_address


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    from uuid import uuid4

    return str(uuid4())


@dataclass
class ApprovalRecord:
    approval_id: str
    wallet_address: str
    specialist_category: str
    wallet_score_decision_id: Optional[str]
    category_score_decision_id: Optional[str]
    formula_name: str
    formula_version: str
    evidence_fingerprint: Optional[str]
    evidence_report_path: Optional[str]
    reviewer: str
    approval_reason: Optional[str]
    approved_at: str
    enabled: bool
    monitoring_enabled: bool
    revoked_at: Optional[str]
    revoked_by: Optional[str]
    revocation_reason: Optional[str]
    created_at: str
    updated_at: str


def normalize_wallet(address: str) -> str:
    """Canonical wallet normalization (lower-case, no surrounding whitespace)."""
    canon = canonical_wallet_address(address)
    if canon is None:
        raise ValueError(f"malformed wallet address: {address!r}")
    return canon


def _row_to_record(row) -> ApprovalRecord:
    data = dict(row)
    return ApprovalRecord(
        approval_id=data["approval_id"],
        wallet_address=data["wallet_address"],
        specialist_category=data["specialist_category"],
        wallet_score_decision_id=data.get("wallet_score_decision_id"),
        category_score_decision_id=data.get("category_score_decision_id"),
        formula_name=data["formula_name"],
        formula_version=data["formula_version"],
        evidence_fingerprint=data.get("evidence_fingerprint"),
        evidence_report_path=data.get("evidence_report_path"),
        reviewer=data["reviewer"],
        approval_reason=data.get("approval_reason"),
        approved_at=data["approved_at"],
        enabled=bool(data["enabled"]),
        monitoring_enabled=bool(data["monitoring_enabled"]),
        revoked_at=data.get("revoked_at"),
        revoked_by=data.get("revoked_by"),
        revocation_reason=data.get("revocation_reason"),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


# A "live" approval is enabled AND not revoked. This is the canonical predicate
# the collector, monitor, and execution consumer use.
def _is_active(row: dict) -> bool:
    return bool(row["enabled"]) and row["revoked_at"] is None


def create_approval(
    db: object,
    *,
    wallet_address: str,
    specialist_category: str,
    reviewer: str,
    formula_name: str,
    formula_version: str,
    wallet_score_decision_id: Optional[str] = None,
    category_score_decision_id: Optional[str] = None,
    evidence_fingerprint: Optional[str] = None,
    evidence_report_path: Optional[str] = None,
    approval_reason: Optional[str] = None,
    monitoring_enabled: bool = True,
) -> ApprovalRecord:
    """Create a durable manual approval.

    Durable invariant: at most one ACTIVE approval per
    (wallet_address, specialist_category, formula_version). The partial unique
    index ``ux_specialist_approvals_active`` enforces this at the DB level; we
    also check first so the caller gets a clear error rather than an integrity
    violation. Revoking/disabling an old approval frees the slot for a new one.
    """
    address = normalize_wallet(wallet_address)
    now = _now_iso()
    # Pre-flight active-check (the unique index is the durable backstop).
    existing = db.conn.execute(
        "SELECT approval_id FROM specialist_approvals "
        "WHERE wallet_address=? AND specialist_category=? AND formula_version=? "
        "AND enabled=1 AND revoked_at IS NULL",
        (address, specialist_category, formula_version),
    ).fetchone()
    if existing is not None:
        raise ValueError(
            f"active approval already exists for "
            f"(wallet={address}, category={specialist_category}, "
            f"version={formula_version}): {existing['approval_id']}"
        )
    approval_id = _uuid()
    db.conn.execute(
        """INSERT INTO specialist_approvals (
            approval_id, wallet_address, specialist_category,
            wallet_score_decision_id, category_score_decision_id,
            formula_name, formula_version, evidence_fingerprint,
            evidence_report_path, reviewer, approval_reason, approved_at,
            enabled, monitoring_enabled, revoked_at, revoked_by,
            revocation_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, NULL, NULL, ?, ?)""",
        (
            approval_id, address, specialist_category,
            wallet_score_decision_id, category_score_decision_id,
            formula_name, formula_version, evidence_fingerprint,
            evidence_report_path, reviewer, approval_reason, now,
            1 if monitoring_enabled else 0, now, now,
        ),
    )
    db.conn.commit()
    return get_approval(db, approval_id)


def get_approval(db: object, approval_id: str) -> ApprovalRecord:
    row = db.conn.execute(
        "SELECT * FROM specialist_approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no approval with id {approval_id}")
    return _row_to_record(row)


def list_approvals(
    db: object,
    *,
    only_active: bool = False,
    wallet_address: Optional[str] = None,
) -> list[ApprovalRecord]:
    sql = "SELECT * FROM specialist_approvals"
    clauses: list[str] = []
    params: list[object] = []
    if only_active:
        clauses.append("enabled=1 AND revoked_at IS NULL")
    if wallet_address is not None:
        clauses.append("wallet_address=?")
        params.append(normalize_wallet(wallet_address))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY approved_at DESC, approval_id"
    rows = db.conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_record(r) for r in rows]


def list_active_approvals(
    db: object, *, wallet_address: Optional[str] = None
) -> list[ApprovalRecord]:
    return list_approvals(db, only_active=True, wallet_address=wallet_address)


def set_enabled(
    db: object, approval_id: str, enabled: bool, *, updated_by: str
) -> ApprovalRecord:
    """Enable/disable an approval. Disabling does NOT delete history."""
    now = _now_iso()
    db.conn.execute(
        "UPDATE specialist_approvals SET enabled=?, updated_at=? "
        "WHERE approval_id=?",
        (1 if enabled else 0, now, approval_id),
    )
    db.conn.commit()
    return get_approval(db, approval_id)


def revoke_approval(
    db: object,
    approval_id: str,
    *,
    revoked_by: str,
    revocation_reason: Optional[str] = None,
) -> ApprovalRecord:
    """Revoke an approval. History is preserved (auditable). Frees the active slot."""
    now = _now_iso()
    db.conn.execute(
        "UPDATE specialist_approvals SET revoked_at=?, revoked_by=?, "
        "revocation_reason=?, enabled=0, updated_at=? WHERE approval_id=?",
        (now, revoked_by, revocation_reason, now, approval_id),
    )
    db.conn.commit()
    return get_approval(db, approval_id)


def get_active_approval(
    db: object, *, wallet_address: str, specialist_category: str, formula_version: str
) -> Optional[ApprovalRecord]:
    """Return the single active approval for the given key, or None."""
    address = normalize_wallet(wallet_address)
    row = db.conn.execute(
        "SELECT * FROM specialist_approvals "
        "WHERE wallet_address=? AND specialist_category=? AND formula_version=? "
        "AND enabled=1 AND revoked_at IS NULL ORDER BY approved_at DESC LIMIT 1",
        (address, specialist_category, formula_version),
    ).fetchone()
    return _row_to_record(row) if row is not None else None
