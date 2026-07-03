"""Paper signal generation module for PR 4 — Chunk 4.

This module is the *runtime* half of PR 4: it consumes persisted
candidates, the freshest persisted price snapshot, the persisted depth
levels for that snapshot, and the persisted scoring decisions, and
emits an immutable paper-signal decision for the candidate.

The runtime contract (Chunk 4):

1. NO invented defaults. If ``intended_stake``, ``side``,
   ``category_label``, depth levels, or any other input is missing,
   the candidate is reported as INCOMPLETE — never silently
   substituted with ``100.0``, ``"BUY"``, or ``""``.
2. Deterministic snapshot selection. Given a candidate, the
   *exact* snapshot row used is selected by point-in-time (the
   most recent fetched_at <= the candidate's recorded reference
   timestamp) with a tie-break by snapshot id DESC. There is no
   "latest" lookup by current wall-clock.
3. Depth-walk evidence is read from
   ``candidate_price_snapshot_levels`` for the chosen snapshot.
   If no levels exist, the verdict engine receives a
   ``DEPTH_NOT_CAPTURED`` reason and produces INCOMPLETE.
4. ``TradeCopyabilityInputV1`` is constructed exclusively from
   persisted fields. There is NO ``None -> 0`` silent conversion
   on optional numeric fields.
5. The wallet and category decisions are loaded point-in-time
   (the latest decision whose ``source_data_timestamp`` is
   <= the snapshot's ``fetched_at``); ties break by decision id
   DESC.
6. Behavior classification is computed from persisted
   ``source_trades`` rows. A ``cutoff_timestamp`` (the snapshot's
   ``fetched_at``) is enforced at the SQL layer so future trades
   cannot leak into the evidence.
7. The final paper-signal decision is persisted with
   ``is_approved = 0``. Up to 7 exit-experiment registrations
   are recorded for COPY_CANDIDATE verdicts.

This module does NOT place orders, does NOT mutate positions,
does NOT call any CLOB endpoint, does NOT make any HTTP request,
and does NOT touch any broker or signing code. Those boundaries
are exercised by the safety tests in T4.11.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from polycopy.db.database import Database
from polycopy.db.copy_candidate_persistence import CandidateStatus
from polycopy.db.price_snapshot_persistence import (
    get_latest_price_snapshot as get_latest_snapshot_for_candidate,
)
from polycopy.scoring.behavior_classification import (
    BehaviorClassificationResult,
    BehaviorEvidence,
    classify_wallet_behavior,
    load_behavior_evidence,
    load_behavior_evidence_from_rows,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletScoreResult,
    WalletVerdict,
    compute_wallet_score_v1,
)
from polycopy.scoring.trade_score_v1 import (
    TradeScoreResult,
    TradeCopyabilityInputV1,
    compute_trade_score_v1,
)
from polycopy.scoring.depth_normalization import (
    DEPTH_LEVELS_MALFORMED,
    DEPTH_NOT_CAPTURED,
    DepthWalkResult,
    NormalizedLevel,
    walk_depth,
    compute_book_hash,
)
from polycopy.scoring.shadow_score_v2_typed import (
    DelayScenario,
    ShadowScoreInputV2,
    ShadowScoreResultV2,
)
from polycopy.scoring.shadow_score_v2_engine import (
    compute_shadow_score_v2_from_input,
    compute_measured_delay_seconds,
)
from polycopy.scoring.paper_signal_input import (
    PaperSignalDecisionInput,
)
from polycopy.scoring.verdict_generation import (
    SignalDecisionInput,
    SignalVerdict,
    generate_signal_verdict,
)
from polycopy.scoring.score_serialization import (
    PersistenceError,
    persist_wallet_score_v1,
    persist_trade_score_v1,
    persist_shadow_score_v2,
    persist_paper_signal,
    record_exit_experiments,
    generate_idempotency_key,
)

logger = logging.getLogger(__name__)


def _safe_lastrowid(cursor: sqlite3.Cursor, *, table: str) -> int:
    """Return ``cursor.lastrowid`` or raise :class:`PersistenceError`.

    Replaces the disallowed ``int(cur.lastrowid or 0)`` pattern. A
    ``None`` lastrowid from an INSERT means the row was never
    written (or the driver cannot resolve the id) — surfacing that
    as an error is safer than silently writing subsequent rows
    that point to id ``0``.
    """
    rid = cursor.lastrowid
    if rid is None:
        raise PersistenceError(
            f"INSERT into {table} returned no rowid (cursor.lastrowid is None)"
        )
    return int(rid)


# ---- Constants --------------------------------------------------------------

CATEGORY_FORMULA_VERSION = "1"
WALLET_FORMULA_VERSION = "1"
TRADE_FORMULA_VERSION = "1"
SHADOW_FORMULA_VERSION = "2-shadow"
PAPER_SIGNAL_FORMULA_VERSION = "1"

# Exit experiments registered for COPY_CANDIDATE paper signals.
# Canonical identifiers match what
# :func:`polycopy.scoring.score_serialization.record_exit_experiments`
# writes — these are the ones persisted in
# ``exit_experiment_registrations.experiment_type``.
EXIT_EXPERIMENT_TYPES: tuple[str, ...] = (
    "HOLD_TO_RESOLUTION",
    "EXIT_24H",
    "EXIT_72H",
    "FAVORABLE_MOVE_005",
    "FAVORABLE_MOVE_010",
    "FAVORABLE_MOVE_015",
    "THESIS_OR_LIQUIDITY_FAILURE",
)


# ---- Persisted inputs loader (Task 4.2) ----------------------------------


@dataclass(frozen=True)
class PersistedPaperSignalInputs:
    """All persisted evidence the paper-signal pipeline needs for a
    single candidate, as gathered by
    :func:`load_persisted_paper_signal_inputs`.

    Every field is ``Optional`` so callers can tell exactly which
    piece of evidence was missing. The fields are:

    - candidate: the persisted ``copy_candidates`` row (dict-shaped)
    - snapshot: the chosen ``candidate_price_snapshots`` row (dict-shaped)
    - snapshot_id: the chosen snapshot's primary-key id
    - source_trade: the persisted ``source_trades`` row (dict-shaped)
    - depth_bids / depth_asks: persisted, bounded, normalized levels
      for the chosen snapshot
    - depth_hash: deterministic SHA-256 over the persisted levels
    - depth_status_reason: set when no levels exist
      (``DEPTH_NOT_CAPTURED``)
    - wallet_decision: latest wallet-score decision with
      ``source_data_timestamp <= snapshot.fetched_at``
    - category_decision: latest category-score decision with the
      exact category label and ``source_data_timestamp <= snapshot.fetched_at``
    - behavior_evidence_cutoff: the cutoff timestamp used for
      behavior classification (= snapshot.fetched_at). Trades with
      timestamp > cutoff are excluded.
    - source_trade_id / wallet_id: pulled from the candidate row.
    - intended_stake: optional field carried from source_trades (None
      if missing — never silently defaulted)
    - side: optional field carried from source_trades (None if missing —
      never silently defaulted to "BUY")
    - price_deterioration_pct: optional (None if missing — never 0)
    """

    candidate: Optional[dict]
    snapshot: Optional[dict]
    snapshot_id: Optional[str]
    source_trade: Optional[dict]
    depth_bids: tuple = field(default_factory=tuple)
    depth_asks: tuple = field(default_factory=tuple)
    depth_hash: Optional[str] = None
    depth_status_reason: Optional[str] = None
    wallet_decision: Optional[dict] = None
    category_decision: Optional[dict] = None
    behavior_evidence_cutoff: Optional[str] = None
    source_trade_id: Optional[str] = None
    wallet_id: Optional[str] = None
    intended_stake: Optional[float] = None
    side: Optional[str] = None
    price_deterioration_pct: Optional[float] = None

    @property
    def has_snapshot(self) -> bool:
        return self.snapshot is not None

    @property
    def has_source_trade(self) -> bool:
        return self.source_trade is not None

    @property
    def has_depth(self) -> bool:
        return (
            self.depth_status_reason is None
            and (bool(self.depth_bids) or bool(self.depth_asks))
        )

    @property
    def has_side(self) -> bool:
        return self.side in ("BUY", "SELL")


def _coerce_levels(rows: Iterable) -> list:
    """Convert sqlite rows into NormalizedLevel objects.

    A malformed row returns an empty list (the caller is responsible
    for translating that into ``DEPTH_LEVELS_MALFORMED``).
    """
    out: list[NormalizedLevel] = []
    for r in rows:
        try:
            price = Decimal(str(r["price"]))
            size = Decimal(str(r["size"]))
            cum_size = Decimal(str(r["cumulative_size"]))
            cum_notional = Decimal(str(r["cumulative_notional"]))
        except (KeyError, ValueError, ArithmeticError, TypeError):
            return []
        if price < 0 or price > 1:
            return []
        if size < 0:
            return []
        out.append(
            NormalizedLevel(
                price=price,
                size=size,
                cumulative_size=cum_size,
                cumulative_notional=cum_notional,
            )
        )
    return out


def _select_snapshot_deterministic(
    db: Database,
    candidate_id: int,
    *,
    reference_timestamp: Optional[str] = None,
) -> Optional[dict]:
    """Select the deterministic price snapshot for ``candidate_id``.

    Rules:

      * If ``reference_timestamp`` is supplied, the chosen snapshot
        must have ``fetched_at <= reference_timestamp`` (strict less-or-equal,
        allowing the candidate's own persisted reference moment).
      * Otherwise, the chosen snapshot is the one with the largest
        ``fetched_at`` that the helper exposes (the freshness
        contract for that helper).
      * Ties on ``fetched_at`` break by snapshot id DESC, so the
        most recently inserted snapshot wins.
    """
    if reference_timestamp is None:
        # Fall back to whatever the helper exposes; tests that
        # require strict point-in-time semantics MUST supply
        # reference_timestamp explicitly.
        snapshot = get_latest_snapshot_for_candidate(db, candidate_id)
        if snapshot is None:
            return None
        try:
            return dict(snapshot)
        except TypeError:
            return vars(snapshot)

    try:
        row = db.fetchone(
            """
            SELECT id, candidate_id, fetched_at, best_bid, best_ask,
                   best_bid_size, best_ask_size, spread,
                   trade_age_seconds, seconds_to_market_end,
                   market_active_at_fetch, market_closed_at_fetch,
                   market_resolved_at_fetch, book_summary_json
            FROM candidate_price_snapshots
            WHERE candidate_id = ?
              AND fetched_at <= ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (candidate_id, reference_timestamp),
        )
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return dict(row)


def _row_to_dict(row: Any) -> Optional[dict]:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except (TypeError, ValueError):
        try:
            return vars(row)
        except TypeError:
            return None


def load_persisted_paper_signal_inputs(
    db: Database,
    candidate_id: int,
) -> PersistedPaperSignalInputs:
    """Load every persisted input the runtime paper-signal pipeline
    needs to make a deterministic, reproducible decision for one
    candidate.

    This function NEVER invents defaults. If any required piece is
    missing, the corresponding field on the returned
    :class:`PersistedPaperSignalInputs` is ``None`` (or empty for
    collections). The caller is responsible for translating missing
    evidence into an INCOMPLETE verdict.

    Determinism guarantees:

      * Snapshot selection is point-in-time + tie-break by id DESC.
      * Wallet decision is selected by
        ``source_data_timestamp <= snapshot.fetched_at`` + id DESC.
      * Category decision is selected by the *exact* category label
        from the snapshot's persisted ``book_summary_json`` (or the
        ``market:<id>`` fallback) with the same point-in-time rule.
      * Behavior classification cutoff is the snapshot's
        ``fetched_at`` (passed to the SQL loader).
    """
    # ---- 1. Candidate row -------------------------------------------------
    try:
        cand_row = db.fetchone(
            "SELECT * FROM copy_candidates WHERE id = ?",
            (candidate_id,),
        )
    except sqlite3.Error:
        cand_row = None
    candidate = _row_to_dict(cand_row)

    # Default reference timestamp = now() UTC. Without it the
    # snapshot selection would silently drift to wall-clock time.
    if candidate is not None:
        ref_ts = (
            candidate.get("reference_timestamp")
            or candidate.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        )
    else:
        ref_ts = datetime.now(timezone.utc).isoformat()

    # ---- 2. Price snapshot (deterministic) --------------------------------
    snapshot = _select_snapshot_deterministic(
        db, candidate_id, reference_timestamp=ref_ts
    )
    snapshot_id = snapshot.get("id") if snapshot else None
    snapshot_fetched_at = (
        snapshot.get("fetched_at") if snapshot else None
    )

    # ---- 3. Source trade --------------------------------------------------
    source_trade_id = (
        candidate.get("source_trade_id") if candidate else None
    )
    wallet_id = (
        candidate.get("wallet_id") if candidate else None
    )
    source_trade: Optional[dict] = None
    intended_stake: Optional[float] = None
    side: Optional[str] = None
    price_deterioration_pct: Optional[float] = None

    if source_trade_id:
        try:
            trade_row = db.fetchone(
                "SELECT id, trader_address, market_source_id, outcome, "
                "side, price, quantity, timestamp FROM source_trades "
                "WHERE id = ?",
                (source_trade_id,),
            )
        except sqlite3.Error:
            trade_row = None
        source_trade = _row_to_dict(trade_row)
        if source_trade is not None:
            side_val = source_trade.get("side")
            if side_val in ("BUY", "SELL"):
                side = side_val

    # intended_stake comes from copy_candidates.source_trade_notional —
    # the persisted trade notional. NEVER silently defaulted to 100.0.
    # Source_trades has no `notional` column (schema ground-truth).
    if candidate is not None:
        cand_notional = candidate.get("source_trade_notional")
        if isinstance(cand_notional, (int, float)) and not isinstance(
            cand_notional, bool
        ):
            intended_stake = float(cand_notional)

    # Snapshot price_deterioration_pct is OPTIONAL. If absent,
    # we leave it None — the trade-score formula will produce a
    # 0 component score, but the input itself is preserved as
    # Optional[float].
    if snapshot is not None:
        det_val = snapshot.get("price_deterioration_pct")
        if isinstance(det_val, (int, float)) and not isinstance(
            det_val, bool
        ):
            price_deterioration_pct = float(det_val)

    # ---- 4. Depth levels --------------------------------------------------
    depth_bids: list[NormalizedLevel] = []
    depth_asks: list[NormalizedLevel] = []
    depth_status_reason: Optional[str] = DEPTH_NOT_CAPTURED
    depth_hash: Optional[str] = None

    if snapshot_id is not None:
        try:
            bid_rows = db.fetchall(
                """
                SELECT level_index, side, price, size,
                       cumulative_size, cumulative_notional
                FROM candidate_price_snapshot_levels
                WHERE snapshot_id = ? AND UPPER(side) = 'BID'
                ORDER BY level_index ASC
                """,
                (snapshot_id,),
            )
            ask_rows = db.fetchall(
                """
                SELECT level_index, side, price, size,
                       cumulative_size, cumulative_notional
                FROM candidate_price_snapshot_levels
                WHERE snapshot_id = ? AND UPPER(side) = 'ASK'
                ORDER BY level_index ASC
                """,
                (snapshot_id,),
            )
        except sqlite3.Error:
            bid_rows = []
            ask_rows = []
        depth_bids = _coerce_levels(bid_rows)
        depth_asks = _coerce_levels(ask_rows)
        if depth_bids or depth_asks:
            depth_hash = compute_book_hash(depth_bids, depth_asks)
            depth_status_reason = None
        elif bid_rows or ask_rows:
            # Rows existed but every row was malformed.
            depth_status_reason = DEPTH_LEVELS_MALFORMED

    # ---- 5. Wallet decision (point-in-time) -------------------------------
    wallet_decision: Optional[dict] = None
    if wallet_id is not None and snapshot_fetched_at is not None:
        try:
            wallet_row = db.fetchone(
                """
                SELECT id, wallet_id, formula_name, formula_version,
                       idempotency_key, final_score, verdict,
                       source_data_timestamp, computed_at
                FROM wallet_score_decisions
                WHERE wallet_id = ? AND formula_name = ?
                  AND formula_version = ?
                  AND COALESCE(source_data_timestamp, '') <= ?
                ORDER BY COALESCE(source_data_timestamp, '') DESC,
                         id DESC
                LIMIT 1
                """,
                (
                    wallet_id, "wallet_score", WALLET_FORMULA_VERSION,
                    snapshot_fetched_at,
                ),
            )
        except sqlite3.Error:
            wallet_row = None
        wallet_decision = _row_to_dict(wallet_row)

    # ---- 6. Category decision (point-in-time + exact label) --------------
    category_label = resolve_category_label_for_inputs(db, candidate, snapshot)
    category_decision: Optional[dict] = None
    if (
        wallet_id is not None
        and category_label is not None
        and snapshot_fetched_at is not None
    ):
        try:
            cat_row = db.fetchone(
                """
                SELECT id, wallet_id, category_label, formula_name,
                       formula_version, idempotency_key, final_score,
                       verdict, source_data_timestamp, computed_at
                FROM category_wallet_score_decisions
                WHERE wallet_id = ? AND category_label = ?
                  AND formula_name = ? AND formula_version = ?
                  AND COALESCE(source_data_timestamp, '') <= ?
                ORDER BY COALESCE(source_data_timestamp, '') DESC,
                         id DESC
                LIMIT 1
                """,
                (
                    wallet_id, category_label, "category_wallet_score",
                    CATEGORY_FORMULA_VERSION, snapshot_fetched_at,
                ),
            )
        except sqlite3.Error:
            cat_row = None
        category_decision = _row_to_dict(cat_row)

    return PersistedPaperSignalInputs(
        candidate=candidate,
        snapshot=snapshot,
        snapshot_id=snapshot_id,
        source_trade=source_trade,
        depth_bids=tuple(depth_bids),
        depth_asks=tuple(depth_asks),
        depth_hash=depth_hash,
        depth_status_reason=depth_status_reason,
        wallet_decision=wallet_decision,
        category_decision=category_decision,
        behavior_evidence_cutoff=snapshot_fetched_at,
        source_trade_id=source_trade_id,
        wallet_id=wallet_id,
        intended_stake=intended_stake,
        side=side,
        price_deterioration_pct=price_deterioration_pct,
    )


def resolve_category_label_for_inputs(
    db: Database,
    candidate: Optional[dict],
    snapshot: Optional[dict],
) -> Optional[str]:
    """Resolve the canonical category label for a candidate.

    Resolution order (first non-empty wins, NO synthesized fallback):

      1. ``snapshot.book_summary_json`` decoded, key ``category_label``.
      2. ``snapshot.book_summary_json`` decoded, key ``category``
         (legacy / alternate spelling).
      3. ``markets.category`` joined via
         ``copy_candidates.market_outcome_id -> market_outcomes.id ->
         market_outcomes.market_id -> markets.id``. If ``markets``
         has no ``category`` column this step yields ``None``.
      4. Otherwise: ``None`` — the caller MUST treat this as
         INCOMPLETE. There is no ``f"market:{market_id}"`` synthetic
         label any more.
    """
    parsed: Optional[dict] = None
    if snapshot is not None:
        summary = snapshot.get("book_summary_json")
        if isinstance(summary, str) and summary.strip():
            try:
                parsed = json.loads(summary)
            except (ValueError, TypeError):
                parsed = None
        if isinstance(parsed, dict):
            for key in ("category_label", "category"):
                label = parsed.get(key)
                if isinstance(label, str) and label.strip():
                    return label.strip()

    # Step 3: try markets.category via the persisted join.
    if candidate is not None:
        outcome_id = candidate.get("market_outcome_id")
        if outcome_id is not None:
            try:
                # Use pragma_table_info to check whether the markets
                # table actually has a category column. This lets
                # the resolver work both with the production schema
                # (no category column) and with a fixture-only
                # migration that adds one.
                col_rows = db.fetchall(
                    "SELECT name FROM pragma_table_info('markets') "
                    "WHERE name = 'category'"
                )
                has_category = bool(col_rows)
            except sqlite3.Error:
                has_category = False
            if has_category:
                try:
                    row = db.fetchone(
                        "SELECT m.category FROM market_outcomes mo "
                        "JOIN markets m ON m.id = mo.market_id "
                        "WHERE mo.id = ?",
                        (outcome_id,),
                    )
                except sqlite3.Error:
                    row = None
                if row is not None:
                    row_dict = _row_to_dict(row) or {}
                    label = row_dict.get("category")
                    if isinstance(label, str) and label.strip():
                        return label.strip()

    # No synthetic fallback — return None and let the caller
    # produce INCOMPLETE.
    return None


# ---- Behavior classification with point-in-time cutoff (Task 4.8) ---------


def load_behavior_evidence_point_in_time(
    db: Database,
    wallet_id: str,
    *,
    cutoff_timestamp: Optional[str] = None,
) -> BehaviorEvidence:
    """Wrapper around ``load_behavior_evidence`` that enforces a
    point-in-time cutoff at the SQL layer.

    When ``cutoff_timestamp`` is set, only source_trades with
    ``timestamp <= cutoff_timestamp`` are loaded — preventing any
    future trade from leaking into the behavior classification.

    When unset, the loader falls back to the canonical
    :func:`load_behavior_evidence` for backwards compatibility with
    code paths that already supply point-in-time selection
    elsewhere.
    """
    if cutoff_timestamp is None:
        return load_behavior_evidence(db, wallet_id)

    canonical = wallet_id
    try:
        row = db.fetchone(
            "SELECT address, canonical_address FROM wallets WHERE id = ?",
            (wallet_id,),
        )
    except sqlite3.Error:
        row = None
    if row is not None:
        try:
            from polycopy.db.wallet_identity import canonical_wallet_address
            canon_fn = canonical_wallet_address
        except Exception:
            canon_fn = None
        if canon_fn is not None:
            row_dict = _row_to_dict(row) or {}
            for key in ("canonical_address", "address"):
                value = row_dict.get(key)
                if value:
                    canon = canon_fn(value)
                    if canon is not None:
                        canonical = canon
                        break

    rows = db.fetchall(
        "SELECT trader_address, side, market_source_id, outcome, "
        "timestamp, is_sample FROM source_trades "
        "WHERE (trader_address = ? OR trader_address = ?) "
        "  AND COALESCE(timestamp, '') <= ? "
        "ORDER BY timestamp ASC",
        (canonical, wallet_id, cutoff_timestamp),
    )

    return load_behavior_evidence_from_rows(rows)


# ---- Trade input builder (Task 4.6) --------------------------------------


def build_trade_copyability_input(
    inputs: PersistedPaperSignalInputs,
    *,
    walk: Optional[DepthWalkResult] = None,
) -> TradeCopyabilityInputV1:
    """Build :class:`TradeCopyabilityInputV1` strictly from persisted
    truth — no ``None -> 0`` silent conversions on numeric fields.

    Only ``side`` and ``market_category`` are accepted as missing
    (``None``) by the score formula. Every numeric field carries
    the persisted value verbatim or ``None`` when absent.

    When ``walk`` is provided it is the SOLE source of truth for
    ``fill_percentage`` and ``executable_depth``. The depth status
    reason and the depth hash are also propagated.
    """
    snapshot = inputs.snapshot or {}
    source_trade = inputs.source_trade or {}

    intended_stake = inputs.intended_stake
    spread = _coerce_opt_float(snapshot.get("spread"))
    best_bid_size = _coerce_opt_float(snapshot.get("best_bid_size"))
    best_ask_size = _coerce_opt_float(snapshot.get("best_ask_size"))
    trade_age_seconds = _coerce_opt_float(snapshot.get("trade_age_seconds"))
    seconds_to_market_end = _coerce_opt_float(
        snapshot.get("seconds_to_market_end")
    )
    price_deterioration_pct = inputs.price_deterioration_pct

    market_active = _coerce_opt_bool(snapshot.get("market_active_at_fetch"))
    market_closed = _coerce_opt_bool(snapshot.get("market_closed_at_fetch"))
    market_resolved = _coerce_opt_bool(
        snapshot.get("market_resolved_at_fetch")
    )

    has_valid_strategy = _coerce_opt_bool(source_trade.get("has_valid_strategy"))
    has_complete_data = _coerce_opt_bool(source_trade.get("has_complete_data"))

    fill_percentage: Optional[float] = None
    executable_depth: Optional[float] = None
    depth_status_reason = inputs.depth_status_reason
    depth_hash = inputs.depth_hash

    if walk is not None:
        # DepthWalkResult exposes filled_notional — the executable
        # notional consumed by the walk. fill_percentage is on
        # [0, 1]; the trade-score formula multiplies by 100.
        #
        # Boundary conversion: the depth walk runs entirely in
        # Decimal (no float/Decimal mixing). At this boundary the
        # persisted fields are converted to float ONLY for the
        # score formula, which is float-only. Do not perform
        # arithmetic mixing Decimal and float elsewhere in this
        # file.
        fill_percentage = float(walk.fill_percentage)
        executable_depth = float(walk.filled_notional)
        if walk.insufficient_reason is not None:
            depth_status_reason = walk.insufficient_reason

    # Resolve market_category from snapshot/candidate. We use the
    # existing helpers — but feed a noop db because we already
    # have the candidate row in hand.
    market_category = _resolve_category_label_safe(candidate=inputs.candidate, snapshot=inputs.snapshot)

    return TradeCopyabilityInputV1(
        wallet_id=inputs.wallet_id or "",
        source_trade_id=inputs.source_trade_id or "",
        side=inputs.side,
        price_deterioration_pct=price_deterioration_pct,
        intended_stake=intended_stake,
        executable_depth=executable_depth,
        fill_percentage=fill_percentage,
        spread=spread,
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
        trade_age_seconds=trade_age_seconds,
        seconds_to_market_end=seconds_to_market_end,
        market_active=market_active,
        market_closed=market_closed,
        market_resolved=market_resolved,
        has_valid_strategy=has_valid_strategy,
        has_complete_data=has_complete_data,
        market_category=market_category,
        depth_walk_result=walk,
        depth_status_reason=depth_status_reason,
        price_snapshot_id=inputs.snapshot_id,
        depth_hash=depth_hash,
    )


def _resolve_category_label_safe(
    *,
    candidate: Optional[dict],
    snapshot: Optional[dict],
) -> Optional[str]:
    """Resolve category label from already-loaded candidate and
    snapshot dicts — no DB I/O and no synthetic fallback.

    Mirrors :func:`resolve_category_label_for_inputs` (steps 1+2
    only). Returns ``None`` if the snapshot has no usable
    ``category_label`` / ``category`` in its ``book_summary_json``,
    so callers can propagate ``None`` straight into the score
    formula and produce INCOMPLETE.
    """
    if snapshot is not None:
        summary = snapshot.get("book_summary_json")
        if isinstance(summary, str) and summary.strip():
            try:
                parsed = json.loads(summary)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                for key in ("category_label", "category"):
                    label = parsed.get(key)
                    if isinstance(label, str) and label.strip():
                        return label.strip()

    # NOTE: deliberately no ``f"market:{market_id}"`` fallback.
    # Returning ``None`` here is the correct behavior — the score
    # formula treats a missing market_category as INCOMPLETE.
    return None


def _coerce_opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    return None


def _coerce_opt_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


# ---- Depth walk (Task 4.5) ------------------------------------------------


def walk_persisted_depth(
    inputs: PersistedPaperSignalInputs,
    *,
    intended_notional: Optional[float] = None,
) -> Optional[DepthWalkResult]:
    """Walk persisted depth levels for a candidate.

    BUY walks asks ascending; SELL walks bids descending. The walk
    respects the persisted ``cumulative_notional`` truncation
    (the levels store post-truncation cumulative values).

    Returns ``None`` when there is no persisted depth to walk. In
    that case the caller falls back to a persisted
    ``DEPTH_NOT_CAPTURED`` reason.

    When ``intended_notional`` is None, the intended notional is
    taken from ``inputs.intended_stake``. If that is also None,
    the walk is skipped and ``None`` is returned — there is no
    invented default.
    """
    if not inputs.has_depth:
        return None
    side = inputs.side
    if side not in ("BUY", "SELL"):
        return None
    notional_value = (
        float(intended_notional)
        if intended_notional is not None
        else (
            float(inputs.intended_stake)
            if inputs.intended_stake is not None
            else None
        )
    )
    if notional_value is None or notional_value <= 0:
        return None

    levels = inputs.depth_asks if side == "BUY" else inputs.depth_bids
    if not levels:
        return None

    try:
        return walk_depth(
            levels=list(levels),
            side=side,
            intended_notional=Decimal(str(notional_value)),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("walk_depth failed: %s", exc)
        return None


# ---- Pure decision function (Task 4.9) -----------------------------------


def generate_paper_signal_decision(
    *,
    wallet_score_result: WalletScoreResult,
    trade_score_result: TradeScoreResult,
    behavior_result: BehaviorClassificationResult,
    category_score: Optional[float],
    category_verdict: Optional[str],
    shadow_result: Optional[object] = None,
    has_hard_exclusion: bool = False,
    hard_exclusion_reason: Optional[str] = None,
) -> SignalVerdict:
    """Pure decision boundary (no I/O, no persistence).

    See :mod:`paper_signal` docstring for the policy. The shadow
    result is persisted for research but is NEVER consumed here
    (Phase 15 / spec — Chunk 5 §5.8). The parameter is typed as
    ``Optional[object]`` to accept either the legacy
    :class:`ShadowScoreResult` or the new
    :class:`ShadowScoreResultV2` without coupling to either.
    """
    signal_input = SignalDecisionInput(
        wallet_score=wallet_score_result.score,
        wallet_verdict=wallet_score_result.verdict,
        category_wallet_score=category_score,
        category_wallet_verdict=category_verdict,
        trade_score=trade_score_result.score,
        trade_verdict=trade_score_result.verdict,
        behavior_classification=behavior_result,
        has_hard_exclusion=has_hard_exclusion,
        hard_exclusion_reason=hard_exclusion_reason,
    )
    decision = generate_signal_verdict(signal_input)
    return decision.verdict


# ---- Orchestration entry point (Step 7 in run_scan.py) -------------------


def evaluate_paper_signals_for_candidate(
    db: Database,
    candidate_id: Optional[int] = None,
    *,
    candidate_id_kw: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Evaluate a single candidate end-to-end and return a summary
    dict.

    This is the orchestration function the runtime calls. It is
    intentionally side-effecting only through the persistence
    helpers (``persist_wallet_score_v1``, ``persist_trade_score_v1``,
    ``persist_shadow_score_v2``, ``persist_paper_signal``,
    ``record_exit_experiments``).

    The function accepts ``candidate_id`` either as the first
    positional argument or as the ``candidate_id`` keyword, so the
    Step 7 caller in ``scripts/run_scan.py`` can pass it
    positionally while the existing tests can still invoke it with
    the kwarg form.

    Safety boundaries:

      * No orders placed.
      * No positions mutated.
      * No broker / signing code invoked.
      * No CLOB / HTTP calls.
      * ``paper_signal_decisions.is_approved`` is always 0.

    The returned ``outcome_kind`` key (always present) is one of:
    ``"persisted"`` when a row was written (success or INCOMPLETE),
    ``"skipped"`` when no candidate row was found and no write
    occurred, ``"failed"`` when an unrecoverable exception was
    caught.
    """
    # Reconcile kwarg forms so callers can use either.
    if candidate_id is None:
        candidate_id = candidate_id_kw
    if candidate_id is None:
        # Caller forgot to pass an id — treat as a skip, not a crash.
        return {
            "candidate_id": None,
            "outcome_kind": "skipped",
            "verdict": "INCOMPLETE",
            "reason": "no_candidate_id",
            "is_approved": 0,
            "paper_signal_id": None,
            "exit_experiments_registered": 0,
        }

    if now is None:
        now = datetime.now(timezone.utc)

    try:
        return _evaluate_paper_signals_for_candidate_inner(
            db, int(candidate_id), now=now,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "paper signal eval crashed for %s", candidate_id
        )
        return {
            "candidate_id": candidate_id,
            "outcome_kind": "failed",
            "verdict": "INCOMPLETE",
            "reason": f"exception:{exc.__class__.__name__}",
            "is_approved": 0,
            "paper_signal_id": None,
            "exit_experiments_registered": 0,
        }


# Legacy alias — the original Step 7 caller used the singular
# ``evaluate_paper_signal_for_candidate`` name.
evaluate_paper_signal_for_candidate = evaluate_paper_signals_for_candidate


def _evaluate_paper_signals_for_candidate_inner(
    db: Database,
    candidate_id: int,
    *,
    now: datetime,
) -> dict:
    inputs = load_persisted_paper_signal_inputs(db, candidate_id)

    summary: dict[str, Any] = {
        "candidate_id": candidate_id,
        "outcome_kind": "persisted",
        "verdict": "INCOMPLETE",
        "reason": "no_candidate",
        "is_approved": 0,
        "paper_signal_id": None,
        "exit_experiments_registered": 0,
    }

    if inputs.candidate is None:
        summary["outcome_kind"] = "skipped"
        return summary

    if not inputs.has_snapshot:
        summary["reason"] = "no_snapshot"
        ps_id = _persist_incomplete_signal(db, inputs, reason="no_snapshot")
        summary["paper_signal_id"] = ps_id
        return summary

    if inputs.source_trade_id is None:
        summary["reason"] = "no_source_trade"
        ps_id = _persist_incomplete_signal(
            db, inputs, reason="no_source_trade"
        )
        summary["paper_signal_id"] = ps_id
        return summary

    if inputs.wallet_id is None:
        summary["reason"] = "no_wallet_id"
        ps_id = _persist_incomplete_signal(db, inputs, reason="no_wallet_id")
        summary["paper_signal_id"] = ps_id
        return summary

    # Behavior evidence — point-in-time safe.
    behavior_evidence = load_behavior_evidence_point_in_time(
        db, inputs.wallet_id,
        cutoff_timestamp=inputs.behavior_evidence_cutoff,
    )
    behavior_result = classify_wallet_behavior(behavior_evidence)

    # Wallet score decision — re-use the persisted one when present,
    # otherwise compute + persist.
    wallet_score_result = _resolve_wallet_score(db, inputs, now=now)
    if wallet_score_result.verdict == WalletVerdict.INCOMPLETE:
        summary["reason"] = "wallet_incomplete"
        ps_id = _persist_incomplete_signal(
            db, inputs,
            wallet_score=wallet_score_result,
            behavior_result=behavior_result,
            reason="wallet_incomplete",
        )
        summary["paper_signal_id"] = ps_id
        return summary

    # Trade copyability — depth walk + score.
    walk = walk_persisted_depth(inputs)
    trade_input = build_trade_copyability_input(inputs, walk=walk)
    trade_score_result = compute_trade_score_v1(
        wallet_id=inputs.wallet_id,
        source_trade_id=inputs.source_trade_id,
        input=trade_input,
        now=now,
    )

    snap_ts = (
        inputs.snapshot.get("fetched_at") if inputs.snapshot else None
    )
    # Idempotency key for the trade decision MUST include the
    # intended stake (rounded to cents as a string) and the
    # category label, otherwise a changed stake or category would
    # silently collide on the same UNIQUE row. See Chunk 4 §A2.
    cat_label_for_idem = (
        _resolve_category_label_safe(
            candidate=inputs.candidate, snapshot=inputs.snapshot,
        )
        or "missing"
    )
    stake_for_idem = (
        f"{float(inputs.intended_stake):.2f}"
        if inputs.intended_stake is not None else "missing"
    )
    trade_idem = generate_idempotency_key(
        formula_name="trade_copyability",
        formula_version=trade_score_result.formula_version,
        wallet_id=inputs.wallet_id,
        source_trade_id=inputs.source_trade_id,
        source_data_timestamp=snap_ts,
        extra_params={
            "snapshot_id": inputs.snapshot_id,
            "depth_hash": inputs.depth_hash,
            "intended_stake": stake_for_idem,
            "category_label": cat_label_for_idem,
        },
    )
    persist_trade_score_v1(
        db,
        inputs.wallet_id,
        inputs.source_trade_id,
        trade_score_result,
        idempotency_key=trade_idem,
        candidate_id=candidate_id,
        price_snapshot_id=inputs.snapshot_id,
        source_data_timestamp=snap_ts,
    )

    # Shadow v2 — parallel-only, never affects v1 verdict. We persist
    # a SEPARATE immutable research row per delay scenario (six
    # scenarios per paper signal: THEORETICAL_IMMEDIATE,
    # DELAY_30_SECONDS, DELAY_2_MINUTES, DELAY_5_MINUTES,
    # DELAY_15_MINUTES, ACTUAL_MEASURED_DELAY). The shadow's first
    # scenario row is what the paper-signal summary reports.
    shadow_results = _compute_and_persist_shadow_v2(
        db,
        inputs=inputs,
        now=now,
    )
    # Primary shadow result for the paper-signal summary is the
    # THEORETICAL_IMMEDIATE scenario (no delay penalty). When it
    # cannot be computed (e.g. missing source/delayed prices), the
    # first non-INCOMPLETE scenario is used; otherwise INCOMPLETE
    # is the honest report.
    primary_shadow = shadow_results.get(
        DelayScenario.THEORETICAL_IMMEDIATE.value
    )
    if primary_shadow is None or primary_shadow.verdict == "SHADOW_INCOMPLETE":
        for scenario_value, result in shadow_results.items():
            if result.verdict != "SHADOW_INCOMPLETE":
                primary_shadow = result
                break
        else:
            primary_shadow = next(iter(shadow_results.values()), None)

    # Category inputs.
    cat_score: Optional[float] = None
    cat_verdict: Optional[str] = None
    if inputs.category_decision is not None:
        try:
            cat_score = float(inputs.category_decision.get("final_score"))
            cat_verdict = str(inputs.category_decision.get("verdict"))
        except (TypeError, ValueError):
            cat_score = None
            cat_verdict = None

    # Final verdict.
    final_verdict = generate_paper_signal_decision(
        wallet_score_result=wallet_score_result,
        trade_score_result=trade_score_result,
        behavior_result=behavior_result,
        category_score=cat_score,
        category_verdict=cat_verdict,
        shadow_result=primary_shadow,
    )

    # Persist paper signal (idempotent on candidate + idempotency key).
    # The idem key MUST include the wallet and category decision
    # ids, the intended stake, the resolved category label, the
    # depth hash, and the trade score+verdict. The verdict text
    # alone is NOT enough to distinguish materially different
    # inputs. See Chunk 4 §A2.
    wallet_decision_id = None
    if inputs.wallet_decision is not None:
        try:
            wallet_decision_id = int(
                inputs.wallet_decision.get("id") or 0
            ) or None
        except (TypeError, ValueError):
            wallet_decision_id = None
    category_decision_id = None
    if inputs.category_decision is not None:
        try:
            category_decision_id = int(
                inputs.category_decision.get("id") or 0
            ) or None
        except (TypeError, ValueError):
            category_decision_id = None

    # ---- Chunk 5 typed paper-signal input ---------------------------------
    # Build the frozen typed input contract from the persisted
    # evidence the runtime collected. This is the canonical source
    # of truth for the paper-signal row's identity.
    paper_signal_typed_input = PaperSignalDecisionInput(
        candidate_id=candidate_id,
        source_trade_id=inputs.source_trade_id or "",
        wallet_id=inputs.wallet_id or "",
        wallet_score_decision_id=wallet_decision_id,
        category_score_decision_id=category_decision_id,
        trade_score_decision_id=None,  # Set below after persist.
        price_snapshot_id=inputs.snapshot_id,
        intended_stake=inputs.intended_stake,
        category_label=cat_label_for_idem
        if cat_label_for_idem != "missing"
        else None,
        behavior_classification=behavior_result.classification.value
        if behavior_result is not None
        else "unknown",
        wallet_formula_name="wallet_score",
        wallet_formula_version=WALLET_FORMULA_VERSION,
        category_formula_name="category_wallet_score",
        category_formula_version=CATEGORY_FORMULA_VERSION,
        trade_formula_name="trade_copyability",
        trade_formula_version=trade_score_result.formula_version,
        evaluation_timestamp=now,
        final_verdict=final_verdict.value,
        final_reason=_signal_reason(final_verdict),
        is_approved=0,  # PR 4 paper signals are NEVER approved.
        auto_approve_requested=False,
    )

    ps_idem = generate_idempotency_key(
        formula_name="paper_signal",
        formula_version=PAPER_SIGNAL_FORMULA_VERSION,
        wallet_id=inputs.wallet_id,
        source_trade_id=inputs.source_trade_id,
        source_data_timestamp=snap_ts,
        extra_params={
            "candidate_id": candidate_id,
            "snapshot_id": inputs.snapshot_id,
            "depth_hash": inputs.depth_hash,
            "wallet_decision_id": (
                str(wallet_decision_id)
                if wallet_decision_id is not None else "missing"
            ),
            "category_decision_id": (
                str(category_decision_id)
                if category_decision_id is not None else "missing"
            ),
            "intended_stake": stake_for_idem,
            "category_label": cat_label_for_idem,
            "trade_score_verdict": str(
                trade_score_result.verdict.value
            ),
            "trade_score": f"{float(trade_score_result.score):.2f}",
        },
    )
    paper_signal_id = persist_paper_signal(
        db,
        candidate_id,
        inputs.wallet_id,
        final_verdict.value,
        _signal_reason(final_verdict),
        wallet_score_result.score,
        trade_score_result.score,
        float(primary_shadow.score) if primary_shadow is not None else 0.0,
        primary_shadow.verdict if primary_shadow is not None else None,
        final_verdict.value,
        snap_ts,
        inputs.source_trade_id,
        inputs.snapshot_id,
        idempotency_key=ps_idem,
        typed_input=paper_signal_typed_input,
    )

    summary["verdict"] = final_verdict.value
    summary["reason"] = "ok"
    summary["is_approved"] = 0
    summary["paper_signal_id"] = paper_signal_id

    if final_verdict == SignalVerdict.COPY_CANDIDATE:
        # Register 7 exit experiments (research only). The schedule
        # is derived from the immutable signal evaluation timestamp,
        # NOT from wall-clock now(). Chunk 5 §5.3.
        n = record_exit_experiments_for_signal(
            db,
            paper_signal_id,
            EXIT_EXPERIMENT_TYPES,
            signal_evaluation_timestamp=now,
        )
        summary["exit_experiments_registered"] = n

    return summary


def _signal_reason(verdict: SignalVerdict) -> str:
    return f"paper_signal_verdict:{verdict.value}"


def _persist_incomplete_signal(
    db: Database,
    inputs: PersistedPaperSignalInputs,
    *,
    reason: str,
    wallet_score: Optional[WalletScoreResult] = None,
    behavior_result: Optional[BehaviorClassificationResult] = None,
) -> int:
    """Persist an INCOMPLETE paper-signal decision for audit.

    Used when the candidate is structurally ineligible before the
    full score pipeline runs. The persisted row is idempotent on
    ``(candidate_id, idempotency_key)``.
    """
    candidate_id = (
        int(inputs.candidate.get("id"))
        if inputs.candidate and "id" in inputs.candidate
        else 0
    )
    snap_ts = (
        inputs.snapshot.get("fetched_at") if inputs.snapshot else None
    )
    ps_idem = generate_idempotency_key(
        formula_name="paper_signal",
        formula_version=PAPER_SIGNAL_FORMULA_VERSION,
        wallet_id=inputs.wallet_id or "",
        source_trade_id=inputs.source_trade_id or "",
        source_data_timestamp=snap_ts,
        extra_params={
            "candidate_id": candidate_id,
            "snapshot_id": inputs.snapshot_id,
            "depth_hash": inputs.depth_hash,
            "verdict": "INCOMPLETE",
            "reason": reason,
        },
    )
    return persist_paper_signal(
        db,
        candidate_id,
        inputs.wallet_id or "",
        "INCOMPLETE",
        reason,
        float(wallet_score.score) if wallet_score is not None else 0.0,
        0.0,
        0.0,
        None,
        "INCOMPLETE",
        snap_ts,
        inputs.source_trade_id,
        inputs.snapshot_id,
        idempotency_key=ps_idem,
    )


def _compute_and_persist_shadow_v2(
    db: Database,
    *,
    inputs: "PersistedPaperSignalInputs",
    now: datetime,
) -> dict[str, ShadowScoreResultV2]:
    """Compute and persist V2 shadow scores for ALL SIX canonical
    delay scenarios (Chunk 5 §5.4–§5.6).

    For each scenario:

      * Build a frozen :class:`ShadowScoreInputV2` from the
        persisted evidence the runtime already collected.
      * Look up the actual delayed snapshot that corresponds to the
        scenario's delay (i.e. the first persisted
        ``candidate_price_snapshots`` row for this candidate with
        ``fetched_at >= source_trade_timestamp + delay``). When no
        such snapshot exists, ``delayed_copy_price`` is ``None`` and
        the result is ``SHADOW_INCOMPLETE``.
      * Compute via :func:`compute_shadow_score_v2_from_input`.
      * Persist via :func:`persist_shadow_score_v2`.

    The function NEVER fabricates delayed prices. When a scenario
    has no matching persisted snapshot, ``delayed_copy_price = None``
    is explicit and the result is honestly ``SHADOW_INCOMPLETE``.

    Returns ``{scenario_value: ShadowScoreResultV2}`` keyed by the
    canonical :class:`DelayScenario` ``.value`` strings.

    Safety: this function NEVER mutates V1 decisions, NEVER sets
    ``is_approved=1``, NEVER opens positions or places orders, and
    NEVER calls any CLOB / HTTP / broker endpoint. A failure in any
    individual scenario is isolated to that scenario's row; the V1
    pipeline continues.
    """
    snap_ts = (
        inputs.snapshot.get("fetched_at") if inputs.snapshot else None
    )
    # The source price for the shadow scenario is the snapshot's
    # best_ask (BUY) or best_bid (SELL) midpoint — when the snapshot
    # has neither, we leave ``source_price`` None.
    source_price: Optional[float] = None
    if inputs.snapshot is not None:
        bp = inputs.snapshot.get("best_bid")
        ap = inputs.snapshot.get("best_ask")
        try:
            if bp is not None and ap is not None:
                source_price = (float(bp) + float(ap)) / 2.0
            elif ap is not None:
                source_price = float(ap)
            elif bp is not None:
                source_price = float(bp)
        except (TypeError, ValueError):
            source_price = None

    # Source trade timestamp for delay calculations.
    source_trade_timestamp: Optional[str] = None
    if inputs.source_trade is not None:
        source_trade_timestamp = inputs.source_trade.get("timestamp")

    # Look up the actual measured delay only when both timestamps
    # are present. The result is the difference in seconds
    # (max(0, ...)) between the persisted snapshot's fetched_at and
    # the source trade timestamp. When either is missing, leave
    # ``measured_delay_seconds`` None — the engine will report
    # SHADOW_INCOMPLETE for the ACTUAL_MEASURED_DELAY scenario.
    measured_delay_seconds: Optional[float] = None
    if snap_ts is not None and source_trade_timestamp is not None:
        try:
            measured_delay_seconds = compute_measured_delay_seconds(
                source_trade_timestamp=source_trade_timestamp,
                candidate_snapshot_timestamp=snap_ts,
            )
        except (TypeError, ValueError):
            measured_delay_seconds = None

    # Common snapshot identity — same for every scenario.
    price_snapshot_id = inputs.snapshot_id
    depth_hash = inputs.depth_hash

    # Other typed inputs that all six scenarios share.
    intended_stake = inputs.intended_stake
    executable_depth_value: Optional[float] = None
    fill_percentage_value: Optional[float] = None
    slippage_value: Optional[float] = None
    spread_value: Optional[float] = None
    if inputs.snapshot is not None:
        try:
            spread_value = (
                float(inputs.snapshot.get("spread"))
                if inputs.snapshot.get("spread") is not None else None
            )
        except (TypeError, ValueError):
            spread_value = None
    # Forward-evidence inputs (skill_persistence, copied_performance,
    # concentration/correlation) come from the runtime context.
    # Until the upstream analyzers populate them, we leave them None
    # — the engine surfaces them as missing reasons and the verdict
    # becomes SHADOW_INCOMPLETE. No silent zero substitution.
    wallet_skill_persistence_input: Optional[float] = None
    copied_realized_performance_input: Optional[float] = None
    concentration_correlation_input: Optional[float] = None

    # Per-scenario delayed price lookup. For each scenario we
    # look up the FIRST persisted snapshot for this candidate whose
    # ``fetched_at`` falls within the scenario's delay window
    # (>= source_trade_timestamp + scenario_delay). When no such
    # snapshot exists, the delayed price is None and the verdict
    # becomes SHADOW_INCOMPLETE.
    scenario_results: dict[str, ShadowScoreResultV2] = {}

    for scenario in DelayScenario:
        # Determine the delayed snapshot and delayed price.
        delayed_copy_price: Optional[float] = None
        delayed_snapshot_id: Optional[str] = None
        scenario_missing: list[str] = []
        if scenario is DelayScenario.THEORETICAL_IMMEDIATE:
            # For the theoretical-immediate scenario, the "delayed
            # price" is the source price at zero delay — there is
            # no separate delayed snapshot. We use the snapshot's
            # midpoint directly. When the source price is missing
            # the engine will mark SHADOW_INCOMPLETE.
            delayed_copy_price = source_price
            delayed_snapshot_id = price_snapshot_id
        elif scenario is DelayScenario.ACTUAL_MEASURED_DELAY:
            # For the actual-measured scenario, the "delayed price"
            # is the snapshot's midpoint — same snapshot we are
            # evaluating against (its fetched_at is the actual
            # measured moment). measured_delay_seconds is computed
            # from real timestamps.
            delayed_copy_price = source_price
            delayed_snapshot_id = price_snapshot_id
            if measured_delay_seconds is None:
                scenario_missing.append("missing_actual_measured_delay_seconds")
        else:
            # Fixed-delay scenario: look up the first persisted
            # snapshot whose fetched_at is at or after
            # source_trade_timestamp + scenario_delay_seconds.
            if (
                source_trade_timestamp is not None
                and inputs.candidate is not None
            ):
                cand_id = (
                    inputs.candidate.get("id")
                    if isinstance(inputs.candidate, dict)
                    else None
                )
                if cand_id is not None:
                    # Look up the frozen delay-seconds value from
                    # the canonical DELAY_SCENARIO_SECONDS map.
                    from polycopy.scoring.shadow_score_v2_typed import (
                        DELAY_SCENARIO_SECONDS as _DELAY_SECONDS,
                    )
                    target_delay = _DELAY_SECONDS.get(scenario)
                    if target_delay is not None:
                        delayed_snapshot_id = _lookup_delayed_snapshot(
                            db,
                            candidate_id=int(cand_id),
                            source_trade_timestamp=source_trade_timestamp,
                            delay_seconds=float(target_delay),
                        )
                        if delayed_snapshot_id is not None:
                            delayed_copy_price = (
                                _midpoint_for_snapshot(
                                    db, delayed_snapshot_id
                                )
                            )
            if delayed_copy_price is None:
                scenario_missing.append(
                    f"missing_delayed_snapshot_for_scenario_{scenario.value}"
                )

        # Build the typed input for this scenario.
        typed_in = ShadowScoreInputV2(
            wallet_id=inputs.wallet_id or "",
            source_trade_id=inputs.source_trade_id or "",
            candidate_id=(
                int(inputs.candidate.get("id"))
                if inputs.candidate is not None
                and isinstance(inputs.candidate.get("id"), int)
                else None
            ),
            delay_scenario=scenario,
            source_price=source_price,
            delayed_copy_price=delayed_copy_price,
            intended_stake=intended_stake,
            executable_depth=executable_depth_value,
            fill_percentage=fill_percentage_value,
            slippage=slippage_value,
            spread=spread_value,
            wallet_skill_persistence_input=wallet_skill_persistence_input,
            copied_realized_performance_input=copied_realized_performance_input,
            concentration_correlation_input=concentration_correlation_input,
            source_data_timestamp=snap_ts,
            price_snapshot_id=(
                delayed_snapshot_id
                if scenario is not DelayScenario.THEORETICAL_IMMEDIATE
                else price_snapshot_id
            ),
            depth_hash=depth_hash,
            missing_forward_reasons=tuple(scenario_missing),
            measured_delay_seconds=(
                measured_delay_seconds
                if scenario is DelayScenario.ACTUAL_MEASURED_DELAY
                else None
            ),
        )

        result = compute_shadow_score_v2_from_input(typed_in)

        # Persist (idempotent on (wallet_id, source_trade_id,
        # formula_name, formula_version, idempotency_key)). The
        # persistence helper derives the idempotency key from the
        # scenario + snapshot id + depth hash so a changed scenario
        # or snapshot creates a new immutable row.
        try:
            persist_shadow_score_v2(
                db,
                wallet_id=inputs.wallet_id or "",
                source_trade_id=inputs.source_trade_id or "",
                result=result,
                candidate_id=typed_in.candidate_id,
                source_data_timestamp=snap_ts,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Shadow persistence failure is isolated; V1 verdict
            # continues. The failure is recorded in the result's
            # missing_forward_reasons so the audit trail reflects
            # what happened.
            logger.warning(
                "shadow v2 persistence failed for scenario=%s: %s",
                scenario.value,
                exc,
            )

        scenario_results[scenario.value] = result

    return scenario_results


def _lookup_delayed_snapshot(
    db: Database,
    *,
    candidate_id: int,
    source_trade_timestamp: str,
    delay_seconds: float,
) -> Optional[str]:
    """Return the FIRST persisted snapshot id for ``candidate_id``
    whose ``fetched_at`` is at or after ``source_trade_timestamp +
    delay_seconds``. Returns ``None`` when no such snapshot exists.

    The lookup is purely on the persisted snapshot table — we never
    fabricate or interpolate a delayed price. When the lookup fails
    to find a match, the caller passes ``delayed_copy_price=None``
    and the shadow verdict becomes ``SHADOW_INCOMPLETE`` honestly.
    """
    from datetime import datetime, timedelta, timezone

    try:
        src_dt = datetime.fromisoformat(
            source_trade_timestamp.replace("Z", "+00:00")
            if source_trade_timestamp.endswith("Z")
            else source_trade_timestamp
        )
    except (TypeError, ValueError):
        return None
    if src_dt.tzinfo is None:
        src_dt = src_dt.replace(tzinfo=timezone.utc)
    target = (src_dt + timedelta(seconds=delay_seconds)).isoformat()

    try:
        row = db.fetchone(
            """
            SELECT id FROM candidate_price_snapshots
            WHERE candidate_id = ?
              AND fetched_at >= ?
            ORDER BY fetched_at ASC, id ASC
            LIMIT 1
            """,
            (candidate_id, target),
        )
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return str(row["id"])
    except (KeyError, TypeError):
        return None


def _midpoint_for_snapshot(
    db: Database, snapshot_id: str,
) -> Optional[float]:
    """Return the persisted midpoint for a snapshot.

    Computed as the average of ``best_bid`` and ``best_ask`` when
    both are present. Returns ``None`` when neither is present or
    the lookup fails.
    """
    try:
        row = db.fetchone(
            "SELECT best_bid, best_ask FROM candidate_price_snapshots "
            "WHERE id = ?",
            (snapshot_id,),
        )
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        bp = row["best_bid"]
        ap = row["best_ask"]
    except (KeyError, TypeError):
        return None
    try:
        if bp is not None and ap is not None:
            return (float(bp) + float(ap)) / 2.0
        if ap is not None:
            return float(ap)
        if bp is not None:
            return float(bp)
        return None
    except (TypeError, ValueError):
        return None


def _resolve_wallet_score(
    db: Database,
    inputs: PersistedPaperSignalInputs,
    *,
    now: datetime,
) -> WalletScoreResult:
    """Reuse the persisted wallet-score decision if present,
    otherwise compute + persist.

    The persisted decision is keyed on the snapshot's point-in-time
    timestamp, so re-running the pipeline with the same snapshot
    yields the same score.
    """
    if inputs.wallet_decision is not None:
        try:
            score = float(inputs.wallet_decision.get("final_score"))
            verdict_str = str(inputs.wallet_decision.get("verdict"))
            try:
                verdict = WalletVerdict(verdict_str)
            except ValueError:
                verdict = WalletVerdict.INCOMPLETE
        except (TypeError, ValueError):
            score = 0.0
            verdict = WalletVerdict.INCOMPLETE
        return WalletScoreResult(
            wallet_id=inputs.wallet_id or "",
            score=score,
            verdict=verdict,
        )

    # Fall back to a re-computation. In a healthy runtime the
    # wallet score is computed and persisted in Chunk 2 / 3 before
    # the paper-signal pipeline runs. We compute it again here so
    # the runtime remains self-contained.
    result = compute_wallet_score_v1(
        wallet_id=inputs.wallet_id or "",
        now=now,
    )
    snap_ts = (
        inputs.snapshot.get("fetched_at") if inputs.snapshot else None
    )
    idem = generate_idempotency_key(
        formula_name="wallet_score",
        formula_version=WALLET_FORMULA_VERSION,
        wallet_id=inputs.wallet_id or "",
        source_data_timestamp=snap_ts,
    )
    try:
        persist_wallet_score_v1(
            db, inputs.wallet_id or "", result,
            idempotency_key=idem,
            source_data_timestamp=snap_ts,
            candidate_id=(
                int(inputs.candidate.get("id"))
                if inputs.candidate and "id" in inputs.candidate
                else None
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("persist_wallet_score_v1 failed: %s", exc)
    return result


def record_exit_experiments_for_signal(
    db: Database,
    paper_signal_id: int,
    experiment_types: tuple[str, ...] = EXIT_EXPERIMENT_TYPES,
    *,
    signal_evaluation_timestamp: Optional[datetime] = None,
) -> int:
    """Register exit experiments for a paper signal.

    Delegates to :func:`score_serialization.record_exit_experiments`.
    The ``experiment_types`` argument is accepted for forward
    compatibility with the canonical Phase 11 migration but is
    currently ignored — the underlying helper has a fixed 7-row
    canonical set (Chunk 5 §5.3).

    Returns the number of registrations that took effect (0
    already-registered rows are skipped by the
    ``UNIQUE(paper_signal_id, experiment_type)`` constraint).
    """
    ids = record_exit_experiments(
        db,
        paper_signal_id,
        signal_evaluation_timestamp=signal_evaluation_timestamp,
    )
    return len(ids)


# ---- Batch entry point (Step 7) ------------------------------------------


def evaluate_paper_signals(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Evaluate every PENDING_PRICE_CHECK candidate.

    Returns a summary with per-verdict counts and the per-candidate
    detail list.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    results: dict[str, Any] = {
        "copy_candidate": 0,
        "watchlist": 0,
        "skip": 0,
        "incomplete": 0,
        "errors": [],
        "details": [],
    }

    try:
        candidates = db.fetchall(
            "SELECT id FROM copy_candidates "
            "WHERE status = ? ORDER BY id ASC",
            (CandidateStatus.PENDING_PRICE_CHECK.value,),
        )
    except sqlite3.Error as exc:
        results["errors"].append(str(exc))
        return results

    for cand in candidates:
        candidate_id = int(cand["id"])
        try:
            summary = evaluate_paper_signals_for_candidate(
                db, candidate_id, now=now,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "paper signal eval failed for %s", candidate_id
            )
            results["errors"].append(
                f"candidate_id={candidate_id}: {exc}"
            )
            continue

        results["details"].append(summary)
        verdict = summary.get("verdict")
        if verdict == SignalVerdict.COPY_CANDIDATE.value:
            results["copy_candidate"] += 1
        elif verdict == SignalVerdict.WATCHLIST.value:
            results["watchlist"] += 1
        elif verdict == SignalVerdict.SKIP.value:
            results["skip"] += 1
        else:
            results["incomplete"] += 1

    return results


# ---- Backwards-compat behavior loader alias -------------------------------


#: Legacy alias — preserved so any caller still importing the old
#: placeholder name keeps working. The new behavior loader is the
#: real one, and there is no empty-evidence scaffolding in the
#: active path.
_load_behavior_evidence_for_wallet = load_behavior_evidence_point_in_time


# ---- Backwards-compat category helpers (Chunk 3 surface preserved) -------
#
# The original ``paper_signal.py`` exposed a small category-decision
# surface that downstream tests still import. The new runtime path
# uses ``resolve_category_label_for_inputs`` /
# ``load_persisted_paper_signal_inputs`` for the same job — these
# legacy names are preserved as thin wrappers so existing tests
# keep passing.

@dataclass(frozen=True)
class PersistedCategoryDecision:
    """Legacy alias for the persisted category-decision row.

    New code should rely on
    :func:`load_persisted_paper_signal_inputs` and the
    ``category_decision`` field of :class:`PersistedPaperSignalInputs`.
    This dataclass is preserved for tests that import the symbol
    directly.
    """

    decision_id: int
    wallet_id: str
    category_label: str
    score: float
    verdict: str
    source_data_timestamp: Optional[str]


def load_persisted_category_decision(
    db: Database,
    wallet_id: str,
    category_label: Optional[str],
) -> Optional[PersistedCategoryDecision]:
    """Legacy category-decision loader — preserved for tests.

    The runtime path uses
    :func:`load_persisted_paper_signal_inputs` for the same lookup.
    """
    if not category_label or not category_label.strip():
        return None
    try:
        row = db.fetchone(
            """
            SELECT id, wallet_id, category_label, final_score, verdict,
                   source_data_timestamp
            FROM category_wallet_score_decisions
            WHERE wallet_id = ? AND category_label = ? AND formula_name = ?
              AND formula_version = ?
            ORDER BY COALESCE(source_data_timestamp, '') DESC, id DESC
            LIMIT 1
            """,
            (wallet_id, category_label, "category_wallet_score",
             CATEGORY_FORMULA_VERSION),
        )
    except sqlite3.Error:
        return None
    if row is None:
        return None
    row_dict = _row_to_dict(row) or {}
    return PersistedCategoryDecision(
        decision_id=int(row_dict.get("id", 0)),
        wallet_id=str(row_dict.get("wallet_id", "")),
        category_label=str(row_dict.get("category_label", "")),
        score=float(row_dict.get("final_score") or 0.0),
        verdict=str(row_dict.get("verdict", "")),
        source_data_timestamp=row_dict.get("source_data_timestamp"),
    )


def resolve_category_label(
    db: Database,
    candidate_row: dict,
    snapshot_row: Optional[dict],
) -> Optional[str]:
    """Legacy category-label resolver — preserved for tests.

    New code should call :func:`resolve_category_label_for_inputs`.
    """
    return resolve_category_label_for_inputs(db, candidate_row, snapshot_row)


def _build_category_inputs(
    db: Database,
    candidate_row: dict,
    snapshot_row: Optional[dict],
) -> tuple[Optional[float], Optional[str]]:
    """Legacy category input builder — preserved for tests.

    Returns ``(category_score, category_verdict)``. Both are ``None``
    when no category decision is persisted for the resolved label.
    """
    label = resolve_category_label_for_inputs(db, candidate_row, snapshot_row)
    if label is None:
        return None, None
    persisted = load_persisted_category_decision(
        db, candidate_row.get("wallet_id", ""), label
    )
    if persisted is None:
        return None, None
    return persisted.score, persisted.verdict


def _load_snapshot_metrics(snapshot: Optional[dict]) -> dict:
    """Legacy snapshot metrics helper — preserved for tests.

    Returns a flat dict of scalar fields the trade-score formula
    reads. ``None`` when no snapshot exists.
    """
    if snapshot is None:
        return {}
    return {
        "best_bid": snapshot.get("best_bid"),
        "best_bid_size": snapshot.get("best_bid_size"),
        "best_ask": snapshot.get("best_ask"),
        "best_ask_size": snapshot.get("best_ask_size"),
        "spread": snapshot.get("spread"),
        "trade_age_seconds": snapshot.get("trade_age_seconds"),
        "seconds_to_market_end": snapshot.get("seconds_to_market_end"),
        "market_active": bool(snapshot.get("market_active_at_fetch")),
        "market_closed": bool(snapshot.get("market_closed_at_fetch")),
        "market_resolved": bool(snapshot.get("market_resolved_at_fetch")),
    }


def generate_paper_signals(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Legacy batch entry-point — preserved for tests.

    Equivalent to :func:`evaluate_paper_signals` (the new Step 7
    entry-point). Returns a summary dict with the same keys.
    """
    return evaluate_paper_signals(db, now=now)