"""Canonical trade → outcome mapping helper (PR-1).

This module is the single source of truth for resolving a
``source_trades`` row to a ``market_outcomes`` row. It exists for three
reasons:

1. **Identity preservation.** Before PR-1, ``source_trades`` only carried
   the denormalized ``outcome`` label (e.g. ``"Yes"``). For multi-outcome
   markets ("Hanwha Eagles", "SSG Landers", "KIA Tigers") that label is
   ambiguous — two markets can share the label ``"Yes"`` and there's no
   way to know which one a trade belongs to without the upstream CLOB
   token id. The data-api emits that id in the ``asset`` field; this PR
   persists it to ``source_trades.token_id`` and the Gamma parser
   persists its positionally-paired counterpart to
   ``market_outcomes.clob_token_id``.

2. **Source-qualified identity.** ``source_trades.source_trade_id`` is
   only unique **within** ``source_trades.source`` — the schema enforces
   ``UNIQUE(source, source_trade_id)``. Two providers can legitimately
   emit the same ``source_trade_id`` string and they must not be
   conflated. ``resolve_trade_to_outcome`` therefore takes both
   ``source`` and ``source_trade_id`` as **required** keyword-only
   arguments; no fallback that searches all sources is allowed, and no
   arbitrary-first-row selection is allowed.

3. **Bounded, observable behavior.** The helper returns one of three
   statuses — ``OK``, ``INCOMPLETE``, ``AMBIGUOUS`` — so downstream code
   (PR-2 and beyond) can branch deterministically instead of silently
   picking an arbitrary row.

Tests-only contract (PR-1 scope):
    This helper is wired up by tests in
    ``tests/test_p01_trade_outcome_identity.py`` only. **No production
    code path calls ``resolve_trade_to_outcome`` in this PR.** Wiring it
    into the live signal path is the explicit goal of PR-2 (copy-
    candidate persistence) and PR-3 (signal generation). Adding a
    production caller here would silently change downstream semantics
    before the rest of the recovery sequence is in place.

Resolution precedence (spec):

    1. **Exact token match.** Join ``source_trades.token_id`` directly to
       ``market_outcomes.clob_token_id``. If exactly one outcome matches,
       return ``OK`` with the full join row. If zero match, return
       ``INCOMPLETE``. If two or more match, return ``AMBIGUOUS`` and
       list every candidate outcome id — never silently pick one.

    2. **Legacy label fallback.** Only when ``source_trades.token_id`` is
       ``NULL``. Join on ``market_source_id`` + normalized outcome label
       (case-insensitive, trimmed). Same ``OK`` / ``INCOMPLETE`` /
       ``AMBIGUOUS`` rules. If ``token_id`` is non-NULL we DO NOT run the
       fallback — token wins, no exceptions.

    3. **Never pick an arbitrary first row.** If the join returns more
       than one row, we surface every candidate id and mark the result
       ambiguous. No ``LIMIT 1`` anywhere.

The label fallback deliberately normalizes whitespace and case so a
legacy v6 row whose ``outcome`` is ``" yes "`` still resolves. Numeric
or token-shaped legacy labels (e.g. ``"1"``) pass through unchanged —
no numeric coercion, because that would invent identity.
"""

from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from polycopy.db.database import Database


class ResolveStatus(str, enum.Enum):
    """Outcome of a ``resolve_trade_to_outcome`` call.

    ``OK``           — exactly one ``market_outcomes`` row matched and
                       every field on ``ResolveResult`` is populated.
    ``INCOMPLETE``   — no row matched. The caller should treat the trade
                       as unresolved (no outcome attribution). May
                       include candidate ids (empty list when truly none).
    ``AMBIGUOUS``    — multiple rows matched. ``candidate_market_outcome_ids``
                       lists every id; the caller MUST NOT auto-pick one.
    """

    OK = "OK"
    INCOMPLETE = "INCOMPLETE"
    AMBIGUOUS = "AMBIGUOUS"


def _normalize_label(label: Any) -> str:
    """Normalize a legacy outcome label for the fallback join.

    Lowercases and trims ASCII whitespace. Empty / non-string values
    collapse to ``""`` so a NULL-ish row never silently matches a real
    outcome.
    """
    if label is None:
        return ""
    if not isinstance(label, str):
        try:
            label = str(label)
        except Exception:
            return ""
    return label.strip().casefold()


def _source_qualified_id(source: str, source_trade_id: str) -> str:
    """Render a source-qualified identity for human-readable audit text.

    Format: ``source='<source>', source_trade_id='<source_trade_id>'``.
    Single-quote characters in either field are escaped by doubling
    (SQL-style) so the string round-trips through log lines safely.
    """
    safe_source = source.replace("'", "''")
    safe_id = source_trade_id.replace("'", "''")
    return f"source='{safe_source}', source_trade_id='{safe_id}'"


@dataclass(frozen=True)
class ResolveResult:
    """Result of ``resolve_trade_to_outcome``.

    The ``source`` and ``source_trade_id`` fields together identify the
    upstream trade under the schema's actual uniqueness contract
    ``UNIQUE(source, source_trade_id)``. They are echoed on every result
    for audit and to make INCOMPLETE / AMBIGUOUS results unambiguous
    about which trade they failed to resolve.

    Fields:

    * ``status``              — see :class:`ResolveStatus`.
    * ``source``              — the input ``source`` value (echoed).
    * ``source_trade_id``     — the input ``source_trade_id`` value (echoed).
    * ``token_id``            — the upstream CLOB token id (echoed). May be
                                ``None`` for trades persisted before v7.
    * ``market_outcome_id``   — populated when ``status == OK`` only.
    * ``market_id``           — populated when ``status == OK`` only.
    * ``market_source_id``    — populated when ``status == OK`` only.
    * ``outcome_label``       — populated when ``status == OK`` only.
    * ``clob_token_id``       — populated when ``status == OK`` only.
    * ``candidate_market_outcome_ids`` — populated when ``status ==
                                AMBIGUOUS``; lists every id that matched.
                                Empty for ``OK`` and ``INCOMPLETE``.
    * ``fallback_used``       — True iff the result came from the legacy
                                label fallback (token was NULL).
    * ``reason``              — short human-readable explanation; useful
                                for log lines and test assertions.
    """

    status: ResolveStatus
    source: str
    source_trade_id: str
    token_id: Optional[str]
    market_outcome_id: Optional[int]
    market_id: Optional[str]
    market_source_id: Optional[str]
    outcome_label: Optional[str]
    clob_token_id: Optional[str]
    candidate_market_outcome_ids: list[int] = field(default_factory=list)
    fallback_used: bool = False
    reason: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status is ResolveStatus.OK

    @property
    def is_incomplete(self) -> bool:
        return self.status is ResolveStatus.INCOMPLETE

    @property
    def is_ambiguous(self) -> bool:
        return self.status is ResolveStatus.AMBIGUOUS


def resolve_trade_to_outcome(
    db: Database,
    *,
    source: str,
    source_trade_id: str,
) -> ResolveResult:
    """Resolve a ``source_trades`` row to its ``market_outcomes`` row.

    See the module docstring for the full precedence contract.

    Args:
        db: a connected :class:`polycopy.db.database.Database`. The
            helper issues only SELECTs against the live schema; no
            writes.
        source: the ``source_trades.source`` value (e.g.
            ``"polymarket_data_api"``). **Required.** ``source_trades``
            is only unique within a ``source``; passing only
            ``source_trade_id`` would risk resolving the wrong row when
            two providers use the same id string.
        source_trade_id: the ``source_trades.source_trade_id`` value to
            resolve (NOT the internal ``id`` UUID). **Required.**

    Returns:
        A :class:`ResolveResult`. Never raises for the canonical error
        paths (missing source_trade row, ambiguous join). Programming
        errors (closed connection, schema mismatch) propagate.
    """
    if not source:
        raise ValueError("source must be a non-empty string")
    if not source_trade_id:
        raise ValueError("source_trade_id must be a non-empty string")

    conn: sqlite3.Connection = db.conn

    # ── Lookup the source_trades row by BOTH identity fields ────────────
    # UNIQUE(source, source_trade_id) is the schema's real identity
    # contract; never query on source_trade_id alone.
    source_row = conn.execute(
        """
        SELECT id, source, source_trade_id, token_id, market_source_id, outcome
        FROM source_trades
        WHERE source = ?
          AND source_trade_id = ?
        """,
        (source, source_trade_id),
    ).fetchone()

    identity = _source_qualified_id(source, source_trade_id)

    if source_row is None:
        return ResolveResult(
            status=ResolveStatus.INCOMPLETE,
            source=source,
            source_trade_id=source_trade_id,
            token_id=None,
            market_outcome_id=None,
            market_id=None,
            market_source_id=None,
            outcome_label=None,
            clob_token_id=None,
            candidate_market_outcome_ids=[],
            fallback_used=False,
            reason=f"source_trades row not found for {identity}",
        )

    raw_token = source_row["token_id"]
    # Empty string is treated as None — SQLite stores TEXT NULL as NULL
    # but legacy ingestion paths sometimes insert empty strings; both
    # forms must skip the exact-token branch.
    token_id: Optional[str] = raw_token if raw_token not in (None, "") else None
    market_source_id = source_row["market_source_id"]
    outcome_label_raw = source_row["outcome"]

    # ── Branch 1: exact token match ────────────────────────────────────────
    if token_id is not None:
        rows = conn.execute(
            """
            SELECT mo.id AS outcome_id,
                   mo.market_id AS market_id,
                   mo.label AS outcome_label,
                   mo.clob_token_id AS clob_token_id,
                   m.source_id AS market_source_id
            FROM market_outcomes mo
            JOIN markets m ON m.id = mo.market_id
            WHERE mo.clob_token_id = ?
            """,
            (token_id,),
        ).fetchall()
        n = len(rows)
        if n == 1:
            row = rows[0]
            return ResolveResult(
                status=ResolveStatus.OK,
                source=source,
                source_trade_id=source_trade_id,
                token_id=token_id,
                market_outcome_id=int(row["outcome_id"]),
                market_id=str(row["market_id"]),
                market_source_id=str(row["market_source_id"]),
                outcome_label=str(row["outcome_label"]),
                clob_token_id=str(row["clob_token_id"]),
                candidate_market_outcome_ids=[],
                fallback_used=False,
                reason=f"exact token match for {identity}",
            )
        if n == 0:
            return ResolveResult(
                status=ResolveStatus.INCOMPLETE,
                source=source,
                source_trade_id=source_trade_id,
                token_id=token_id,
                market_outcome_id=None,
                market_id=None,
                market_source_id=None,
                outcome_label=None,
                clob_token_id=None,
                candidate_market_outcome_ids=[],
                fallback_used=False,
                reason=(
                    f"token_id present but no market_outcomes.clob_token_id "
                    f"matches for {identity}"
                ),
            )
        # n >= 2 — ambiguous by construction (the token is non-unique
        # across markets, which itself is a data integrity issue, but we
        # surface every candidate rather than picking one).
        return ResolveResult(
            status=ResolveStatus.AMBIGUOUS,
            source=source,
            source_trade_id=source_trade_id,
            token_id=token_id,
            market_outcome_id=None,
            market_id=None,
            market_source_id=None,
            outcome_label=None,
            clob_token_id=None,
            candidate_market_outcome_ids=[int(r["outcome_id"]) for r in rows],
            fallback_used=False,
            reason=(
                f"token_id matched {n} market_outcomes rows for {identity}; "
                "explicit AMBIGUOUS, no arbitrary selection"
            ),
        )

    # ── Branch 2: legacy label fallback ────────────────────────────────────
    # Only when token_id IS NULL. Same OK/INCOMPLETE/AMBIGUOUS rules.
    normalized = _normalize_label(outcome_label_raw)
    if not normalized:
        # No usable label either — truly unresolved.
        return ResolveResult(
            status=ResolveStatus.INCOMPLETE,
            source=source,
            source_trade_id=source_trade_id,
            token_id=None,
            market_outcome_id=None,
            market_id=None,
            market_source_id=market_source_id,
            outcome_label=str(outcome_label_raw) if outcome_label_raw is not None else None,
            clob_token_id=None,
            candidate_market_outcome_ids=[],
            fallback_used=True,
            reason=(
                f"token_id is NULL and outcome label is empty/whitespace; "
                f"no fallback join possible for {identity}"
            ),
        )

    rows = conn.execute(
        """
        SELECT mo.id AS outcome_id,
               mo.market_id AS market_id,
               mo.label AS outcome_label,
               mo.clob_token_id AS clob_token_id,
               m.source_id AS market_source_id
        FROM market_outcomes mo
        JOIN markets m ON m.id = mo.market_id
        WHERE m.source_id = ?
          AND LOWER(TRIM(mo.label)) = ?
        """,
        (market_source_id, normalized),
    ).fetchall()
    n = len(rows)
    if n == 1:
        row = rows[0]
        return ResolveResult(
            status=ResolveStatus.OK,
            source=source,
            source_trade_id=source_trade_id,
            token_id=None,
            market_outcome_id=int(row["outcome_id"]),
            market_id=str(row["market_id"]),
            market_source_id=str(row["market_source_id"]),
            outcome_label=str(row["outcome_label"]),
            clob_token_id=row["clob_token_id"],  # may be NULL pre-PR-1
            candidate_market_outcome_ids=[],
            fallback_used=True,
            reason=f"legacy label fallback resolved one outcome for {identity}",
        )
    if n == 0:
        return ResolveResult(
            status=ResolveStatus.INCOMPLETE,
            source=source,
            source_trade_id=source_trade_id,
            token_id=None,
            market_outcome_id=None,
            market_id=None,
            market_source_id=market_source_id,
            outcome_label=str(outcome_label_raw) if outcome_label_raw is not None else None,
            clob_token_id=None,
            candidate_market_outcome_ids=[],
            fallback_used=True,
            reason=(
                f"token_id is NULL and no market_outcome matches the "
                f"normalized label under the same market_source_id for {identity}"
            ),
        )
    # n >= 2 — ambiguous fallback (two outcomes in the same market with
    # the same normalized label). This is a real upstream data issue;
    # we surface every candidate id rather than picking one.
    return ResolveResult(
        status=ResolveStatus.AMBIGUOUS,
        source=source,
        source_trade_id=source_trade_id,
        token_id=None,
        market_outcome_id=None,
        market_id=None,
        market_source_id=market_source_id,
        outcome_label=str(outcome_label_raw) if outcome_label_raw is not None else None,
        clob_token_id=None,
        candidate_market_outcome_ids=[int(r["outcome_id"]) for r in rows],
        fallback_used=True,
        reason=(
            f"legacy label fallback matched {n} outcomes under the same "
            f"market_source_id for {identity}; explicit AMBIGUOUS, "
            "no arbitrary selection"
        ),
    )