"""run_scan wiring helpers — PR 5 of 6 (wire paper pilot decision pipeline).

This module is the smallest safe bridge between the discovery / scoring loop
that already runs in :mod:`scripts.run_scan` and the PR #17 / PR 4 evidence
tables:

    * ``wallet_score_decisions``
    * ``category_wallet_score_decisions``
    * ``copy_candidates``  (``evaluate_source_trade_for_wallet`` +
      ``persist_copy_candidate``)
    * ``decision_verdicts``
    * ``score_component_inputs``

It does NOT touch:

    * ``paper_signal_decisions`` — the existing Step 7 path
      (``scripts.run_scan._evaluate_paper_signals_step`` →
      :func:`polycopy.scoring.paper_signal.evaluate_paper_signal_for_candidate`)
      owns persistence of paper-signal rows and the seven exit-experiment
      tracks (those are recorded only after a COPY_CANDIDATE paper signal
      is created by the existing pipeline).
    * ``shadow_decisions``  — written by the existing paper-signal pipeline.
    * ``orders``, ``positions``, ``trades``, ``fills`` — the runtime never
      touches them.

The wiring is intentionally paper-only and idempotent. Repeated scans of the
same wallet/trade/set of inputs produce the same persisted rows (same
idempotency key) instead of duplicating semantic decisions.

Runtime safety:

    * ``max_paper_candidates`` caps the total persisted copy-candidate rows.
    * ``max_trades_per_wallet`` caps the trades considered for candidate
      generation per wallet.

These knobs keep scan runtime bounded even when the wallet registry grows.
Bounded processing is the chosen mitigation for the scan-runtime concern
flagged in the PR 5 charter; raising systemd ``TimeoutStartSec`` is NOT
used.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from polycopy.db.copy_candidate_persistence import (
    evaluate_source_trade_for_wallet,
    persist_copy_candidate,
)
from polycopy.db.database import Database
from polycopy.db.wallet_identity import canonical_wallet_address
from polycopy.domain.copyability import (
    CopyabilityScore,
    DataQuality,
    MissingField,
    ScoreComponent,
    Verdict,
)
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet
from polycopy.scoring.paper_signal import WALLET_FORMULA_VERSION
from polycopy.scoring.score_serialization import (
    persist_category_score_v1,
    persist_wallet_score_v1,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletScoreInputV1,
    compute_wallet_score_v1,
)
from polycopy.scoring.category_wallet_score_v1 import (
    CategoryWalletScoreInputV1,
    compute_category_wallet_score_v1,
)

logger = logging.getLogger(__name__)


# ---- Result counters (Step 5b / 5c / 5d / 5e) ---------------------------

class ScanPipelineCounters:
    """Counters for the PR-5 pipeline writes. Always read-only to callers."""

    __slots__ = (
        "wallet_score_decisions_persisted",
        "wallet_score_decisions_reused",
        "wallet_scores_deferred",
        "category_score_decisions_persisted",
        "category_score_decisions_reused",
        "copy_candidates_created",
        "copy_candidates_rejected_wallet",
        "copy_candidates_rejected_other",
        "decision_verdicts_persisted",
        "score_component_inputs_persisted",
        "trades_scanned_for_candidates",
        # Internal: fresh-insert wallet IDs from Step 5b so Step 5e can
        # scope audit writes to the wallets that received a NEW row in
        # this run (skip-already-scored wallets are excluded, deferred
        # wallets are excluded). Not part of the public counters surface
        # — callers should use it via ``counters._fresh_insert_wallet_ids``.
        "_fresh_insert_wallet_ids",
    )

    def __init__(self) -> None:
        # PR-17 wallet-score table
        self.wallet_score_decisions_persisted: int = 0
        self.wallet_score_decisions_reused: int = 0
        # PR 5 bounded-progression telemetry. ``wallet_scores_deferred``
        # is the number of wallets that ``metrics_by_address`` discovered
        # but were skipped because ``max_wallet_scores`` was already
        # consumed by fresh inserts in this run. Deferred wallets
        # remain eligible for the next scan (sorted iteration order is
        # stable, so the next run continues where this one stopped).
        self.wallet_scores_deferred: int = 0
        # PR-17 category-score table
        self.category_score_decisions_persisted: int = 0
        self.category_score_decisions_reused: int = 0
        # PR-2 / PR-17 copy-candidate table
        self.copy_candidates_created: int = 0
        self.copy_candidates_rejected_wallet: int = 0
        self.copy_candidates_rejected_other: int = 0
        # V12 audit trail
        self.decision_verdicts_persisted: int = 0
        self.score_component_inputs_persisted: int = 0
        # Provenance
        self.trades_scanned_for_candidates: int = 0
        # Internal — set by ``persist_wallet_v1_decisions`` so Step 5e
        # can scope audit writes to exactly the wallets this run
        # inserted a fresh row for. Initialized here so ``as_dict()``
        # works before the helper runs.
        self._fresh_insert_wallet_ids: list[str] = []

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


# ------------------------------------------------------------------------
# PR 19 — bounded Step 5 (legacy scoring loop) slice resolution
# ------------------------------------------------------------------------
#
# ``Step 5`` in ``scripts/run_scan.py`` historically iterated the FULL
# discovery wallet list to compute metrics + call ``evaluate_wallet``
# for the legacy tally counters. With a 95k+ wallet corpus this loop
# alone consumes the systemd ``TimeoutStartSec=900`` budget before
# PR #18's bounded Step 5b ever runs, and no PR-18 evidence rows are
# produced.
#
# The fix below resolves the SAME bounded-progression concept PR #18
# used for Step 5b but applied to the upstream legacy loop:
#
#   1. Sort canonical addresses once (deterministic iteration order).
#   2. Pre-load the existing ``(wallet_id, idempotency_key)`` set so we
#      can recognise already-scored material-identical wallets without
#      a second DB scan.
#   3. Return a slice = (fresh inserts the budget allows) +
#      already-scored no-op addresses for honest telemetry. Deferred
#      addresses are NOT in the slice.
#
# Because skipped (already-scored) wallets do NOT consume the budget,
# the scan naturally rotates through the corpus across runs: the next
# scan sees the previously-deferred addresses arrive at the budget
# head.
# ------------------------------------------------------------------------


@dataclass
class SlicedRunTelemetry:
    """Telemetry emitted by :func:`resolve_bounded_wallet_slice`.

    Hard-cap invariant
    ------------------
    ``len(addresses_in_slice)`` is ALWAYS bounded by
    ``max_wallet_scores`` when ``max_wallet_scores`` is a positive
    integer (or by the corpus size when no cap is configured). This
    invariant is enforced inside the helper; the dataclass exposes the
    underlying counts so operators can observe the rotation without
    having to read the helper.

    Field semantics
    ---------------
    All counts are integers. ``addresses_in_slice`` is a ``list[str]``
    of canonical addresses the caller should iterate downstream; it is
    always a subsequence of the sorted input addresses and obeys the
    hard-cap invariant above.
    """

    addresses_in_slice: list[str]
    # Total canonical addresses seen (the denominator — full corpus size
    # after dedupe, before slicing).
    wallets_considered: int
    # How many of the in-slice addresses are FRESH (no prior
    # wallet_score_decisions row for this wallet_id). Fresh addresses
    # consume budget. ``wallets_in_slice_fresh`` is the budget-burning
    # count.
    wallets_in_slice_fresh: int
    # How many of the in-slice addresses are ALREADY-SCORED (a prior
    # wallet_score_decisions row exists for this wallet_id). Already-
    # scored slots are zero-budget no-ops as far as Step 5b is
    # concerned; they are included in the slice ONLY to fill the cap
    # because the corpus has fewer fresh addresses than the cap. They
    # DO NOT cause downstream Steps 5b / 5c / 5d / 5e to do extra work
    # — Step 5b short-circuits via the existing skip-already-scored
    # pre-flight cache.
    wallets_in_slice_already_scored: int
    # sum(wallets_in_slice_fresh + wallets_in_slice_already_scored),
    # asserted equal to len(addresses_in_slice) at every return site.
    wallets_in_slice_total: int
    # Remainder: corpus minus in-slice. Deferred wallets are processed
    # on a subsequent run as the sorted cursor advances.
    wallets_deferred_to_next_run: int


def _resolve_wallet_id_or_none(
    db: Database, canonical_addr: str,
) -> Optional[str]:
    """Wrapper used by :func:`resolve_bounded_wallet_slice` to look up the
    ``wallets.id`` UUID for a canonical address.

    Returns ``None`` if the wallet is not yet present in the ``wallets``
    table; the caller treats this as "not scored yet" and routes it
    through the fresh-slot path.
    """
    return _load_wallet_id(db, canonical_addr)


def resolve_bounded_wallet_slice(
    db: Database,
    *,
    addresses: Sequence[str],
    max_wallet_scores: Optional[int],
) -> SlicedRunTelemetry:
    """Return the bounded wallet slice the legacy Step 5 should iterate.

    Hard-cap invariant
    ------------------
    When ``max_wallet_scores`` is a positive integer:

    .. code-block:: python

        len(addresses_in_slice) <= max_wallet_scores

    ALWAYS. This holds across repeated runs and across deployments
    where a growing share of the corpus carries prior V1 wallet_score
    decisions. The legacy Step 5 loop therefore operates on at most
    ``max_wallet_scores`` addresses per run, no matter the corpus
    size or how many of those addresses are already scored. This is
    the runtime bound that keeps the scan inside the systemd
    ``TimeoutStartSec=900`` budget.

    Selection algorithm
    -------------------
    Walks the **sorted canonical-address list** once. The slice is
    built in two passes so fresh wallets always win the budget:

      1. **First pass — fresh wallets**: every address that has *no*
         prior ``wallet_score_decisions`` row for its ``wallet_id``
         (under the V1 formula) is appended to the slice until the
         cap is reached. These consume budget.

      2. **Second pass — fill to cap with already-scored no-ops**:
         if the slice is still shorter than the cap after step 1,
         already-scored addresses are appended in sorted order until
         ``len(addresses_in_slice) == max_wallet_scores``. Already-
         scored slots are zero-budget fillers and are included so the
         operator-facing summary stays accurate, but they cost nothing
         in Step 5b (the existing skip-already-scored pre-flight
         short-circuits).

      3. **Defer the rest**: addresses neither fresh nor already-
         scored (i.e. the cap-excluded remainder) are counted as
         deferred.

    Why fresh wins
    -------------
    Fresh wallets are the only ones that actually do work in
    Steps 5b–5e (a brand-new wallet_score_decisions row + downstream
    category / copyability / signal rows). Already-scored wallets are
    verified no-ops in Step 5b. Putting fresh first guarantees the
    cap is used for the work that has to happen.

    Material-input bypass prevention
    --------------------------------
    PR-19 v1 had a flaw: a previously-scored wallet whose material
    inputs later change could enter the slice as an "already-scored"
    zero-budget filler and then write a fresh row downstream. The
    hard-cap invariant still holds (the new row is still bounded
    inside the cap), but the operator could observe the rotation
    without realising the material content had changed. The current
    implementation therefore ALSO recognises the material-change
    case: any address whose wallet_id already has a prior
    wallet_score_decisions row is treated as ``fresh`` (budget-
    consuming) the FIRST run after the row appears, and as
    ``already-scored`` only on runs where no later material has been
    written. Concretely — the helper detects material change cheaply
    by comparing the wallet's latest known
    ``source_trades.timestamp`` against the latest
    ``wallet_score_decisions.source_data_timestamp`` for that
    wallet_id; a mismatch means material inputs have moved and the
    wallet is upgraded from ``already_scored`` to ``fresh`` for this
    run.

    Parameters
    ----------
    db:
        Connected :class:`polycopy.db.database.Database`.
    addresses:
        Iterable of canonical wallet addresses to slice. May be
        unsorted; the helper sorts internally.
    max_wallet_scores:
        Hard cap on ``len(addresses_in_slice)``. ``None`` or a
        non-positive value returns the full sorted list (no cap), with
        already-scored wallets pre-marked for downstream short-circuit.

    Returns
    -------
    SlicedRunTelemetry
        Telemetry dataclass carrying the slice, the per-category
        counts, and an enforcement ``len(addresses_in_slice) <= cap``
        on every non-uncapped call.

    Raises
    ------
    AssertionError
        If the invariant ``len(addresses_in_slice) <= cap`` (when cap
        is configured) is violated by an internal bug. The assertion
        exists as a belt-and-braces guard — the algorithm guarantees
        it, but the assertion makes any future regression fail loudly.
    """
    sorted_addrs = sorted(set(addresses))
    cap: Optional[int] = (
        max_wallet_scores if (max_wallet_scores and max_wallet_scores > 0) else None
    )

    # Resolve wallet_ids up front + pre-load the existing
    # (wallet_id, idempotency_key) set. This is the same pre-flight
    # cache the inner loop of ``persist_wallet_v1_decisions`` uses;
    # we keep it here so the slice decision does not need a second
    # DB scan and never disagrees with what Step 5b will do.
    addr_to_wallet_id: dict[str, Optional[str]] = {
        canonical_addr: _resolve_wallet_id_or_none(db, canonical_addr)
        for canonical_addr in sorted_addrs
    }
    candidate_wallet_ids = [
        wid for wid in addr_to_wallet_id.values() if wid is not None
    ]
    existing_keys = _existing_wallet_idem_keys(
        db, candidate_wallet_ids, WALLET_FORMULA_VERSION,
    )
    fresh_set, already_scored_set, material_changed_set = _classify_wallet_state(
        db, addr_to_wallet_id, existing_keys, sorted_addrs,
    )

    # Build the in-slice address list under the hard cap. Fresh and
    # material-changed consume budget; already-scored fill remaining
    # slots only if the cap is not already saturated by fresh work.
    cap_remaining: Optional[int] = cap
    addresses_in_slice: list[str] = []
    selected_fresh: list[str] = []
    selected_already_scored: list[str] = []

    def _append(addr: str, *, fresh: bool) -> None:
        """Append ``addr`` to the slice if budget permits.

        Honors the hard cap. Raises ``IndexError`` only for caller
        misuse (passing ``fresh=True`` after the cap is exhausted
        without explicit override); the public path always respects
        the cap.
        """
        nonlocal cap_remaining
        if cap_remaining is None:
            addresses_in_slice.append(addr)
            return
        if cap_remaining <= 0:
            return
        addresses_in_slice.append(addr)
        cap_remaining -= 1
        if fresh:
            selected_fresh.append(addr)
        else:
            selected_already_scored.append(addr)

    # Pass 1 — fresh and material-changed wallets (budget-consuming).
    for canonical_addr in sorted_addrs:
        if canonical_addr in fresh_set or canonical_addr in material_changed_set:
            _append(canonical_addr, fresh=True)

    # Pass 2 — already-scored wallets (zero-budget fillers).
    for canonical_addr in sorted_addrs:
        if canonical_addr in already_scored_set:
            _append(canonical_addr, fresh=False)

    addresses_in_slice.sort()  # canonical order for downstream

    deferred_total = max(0, len(sorted_addrs) - len(addresses_in_slice))

    # Enforce the hard-cap invariant. The algorithm guarantees it;
    # the assertion makes any future regression fail loudly.
    if cap is not None:
        assert len(addresses_in_slice) <= cap, (
            f"resolve_bounded_wallet_slice violated its own hard-cap: "
            f"len(addresses_in_slice)={len(addresses_in_slice)} "
            f"> cap={cap}"
        )
    assert len(selected_fresh) + len(selected_already_scored) == len(
        addresses_in_slice,
    )
    # Note: it is valid for a material-changed address to NOT be in
    # ``selected_fresh`` if the cap ran out before reaching it in
    # pass 1. The cap-truncation invariant is what protects us
    # against unbounded expansion; that invariant is the assertion
    # below on ``len(addresses_in_slice) <= cap``.
    assert (
        len(selected_fresh) + len(selected_already_scored) == len(addresses_in_slice)
    ), (
        "fresh (incl. material-changed) + already-scored must equal "
        "addresses_in_slice length"
    )
    # The slice invariant for material-change bypass prevention: every
    # material-changed address that appears in the slice must be in
    # ``selected_fresh`` (it budget-consumes). An address that did
    # NOT make the slice (because the cap ran out) cannot be in
    # either list, which is fine — it deferred.
    for addr in material_changed_set & set(addresses_in_slice):
        assert addr not in selected_already_scored, (
            f"material-changed {addr} must not be classified as "
            f"already-scored; that would defeat the bypass prevention"
        )

    return SlicedRunTelemetry(
        addresses_in_slice=addresses_in_slice,
        wallets_considered=len(sorted_addrs),
        wallets_in_slice_fresh=len(selected_fresh),
        wallets_in_slice_already_scored=len(selected_already_scored),
        wallets_in_slice_total=len(addresses_in_slice),
        wallets_deferred_to_next_run=deferred_total,
    )


def _classify_wallet_state(
    db: Database,
    addr_to_wallet_id: dict[str, Optional[str]],
    existing_keys: set[str],
    sorted_addrs: list[str],
) -> tuple[set[str], set[str], set[str]]:
    """Return (fresh_set, already_scored_set, material_changed_set).

    * ``fresh_set`` — wallet has NO prior ``wallet_score_decisions``
      row under V1 (or has no row in the ``wallets`` table yet).
    * ``already_scored_set`` — wallet has a prior V1 row AND the latest
      source_trades timestamp for this wallet matches the latest
      ``source_data_timestamp`` of the wallet_score_decisions row.
      Treat as zero-budget no-op.
    * ``material_changed_set`` — wallet has a prior V1 row BUT the
      latest source_trades timestamp is newer than the latest
      ``source_data_timestamp``. Treat as fresh (budget-consuming) on
      this run so the strict material check downstream actually has a
      chance to write a new row.

    # The timestamp cross-check is the cheap material-proxy: we never
    # recompute wallet metrics inside the slice helper, but we still
    # distinguish "wallet has not changed since last scoring" from
    # "wallet has moved on since last scoring". A wallet whose
    # source_trades timestamp has not advanced since its last V1 score is
    # a strict-material no-op by construction (its canonical metrics
    # blob is identical); a wallet whose source_trades have advanced
    # may still produce an identical canonical blob (e.g. only trade
    # prices ticked without changing aggregates), but the safer
    # conservative default is to budget-consume it and let Step 5b's
    # strict material check decide.

    Notes
    -----
    ``source_data_timestamp`` on the V1 row and ``timestamp`` on the
    source_trades row are ISO-8601 strings that may differ in
    designator (``Z`` vs ``+00:00``) for the same instant. We
    normalise both to ``+00:00`` before comparing so ``Z``-stamped
    trade rows are not falsely classified as "newer than" ``+00:00``-
    stamped V1 rows from the same instant.
    """

    def _iso_z_to_plus(ts: str) -> str:
        """Return ISO string with ``Z`` normalised to ``+00:00``.

        Both are UTC offsets; the same instant can carry either form.
        Plain string compare would treat ``Z`` (0x5A) as greater than
        ``+`` (0x2B) for the same wall-clock, which would falsely
        flag every V1 row whose ``source_data_timestamp`` uses
        ``+00:00`` as material-stale. Normalising both sides keeps
        the proxy semantically correct.
        """
        if not isinstance(ts, str):
            return ""
        return ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    # Pre-load (wallet_id, latest_source_data_timestamp) for any
    # wallet_id that has prior decisions.
    wallet_ids_with_prior = {
        wid
        for wid in addr_to_wallet_id.values()
        if wid is not None
        and any(combo.startswith(f"{wid}|") for combo in existing_keys)
    }
    last_scored_ts: dict[str, str] = {}
    if wallet_ids_with_prior:
        wid_placeholders = ",".join("?" for _ in wallet_ids_with_prior)
        try:
            rows = db.fetchall(
                f"SELECT wallet_id, MAX(source_data_timestamp) AS ts "
                f"FROM wallet_score_decisions "
                f"WHERE wallet_id IN ({wid_placeholders}) "
                f"AND formula_name = 'wallet_score' "
                f"AND formula_version = '1' "
                f"GROUP BY wallet_id",
                tuple(wallet_ids_with_prior),
            )
        except Exception:  # noqa: BLE001 — defensive: never abort slice
            rows = []
        for row in rows:
            wid = row["wallet_id"]
            ts = row["ts"]
            if wid is not None:
                last_scored_ts[str(wid)] = _iso_z_to_plus(
                    str(ts) if ts is not None else "",
                )

    # Pre-load (canonical_address, MAX(timestamp)) over source_trades
    # for the candidate addresses. Used as the cheap material-change
    # proxy.
    last_trade_ts: dict[str, str] = {}
    if sorted_addrs:
        addr_placeholders = ",".join("?" for _ in sorted_addrs)
        try:
            rows = db.fetchall(
                f"SELECT trader_address, MAX(timestamp) AS ts "
                f"FROM source_trades "
                f"WHERE trader_address IN ({addr_placeholders}) "
                f"GROUP BY trader_address",
                tuple(sorted_addrs),
            )
        except Exception:  # noqa: BLE001 — defensive: never abort slice
            rows = []
        # Map canonical_address -> wallet_id so we can group by wid.
        addr_to_last_trade = {
            row["trader_address"]: str(row["ts"] or "")
            for row in rows
            if row["trader_address"]
        }
        for canonical_addr in sorted_addrs:
            wid = addr_to_wallet_id.get(canonical_addr)
            if wid is None or wid not in wallet_ids_with_prior:
                continue
            t = addr_to_last_trade.get(canonical_addr)
            if t is None:
                continue
            # Use normalised comparison (Z vs +00:00 are the same instant
            # but compare unequal as raw strings).
            t_norm = _iso_z_to_plus(t)
            cur = last_trade_ts.get(wid)
            if cur is None or t_norm > cur:
                last_trade_ts[wid] = t_norm

    fresh_set: set[str] = set()
    already_scored_set: set[str] = set()
    material_changed_set: set[str] = set()
    for canonical_addr in sorted_addrs:
        wallet_id = addr_to_wallet_id.get(canonical_addr)
        if wallet_id is None:
            # No ``wallets`` row yet — definitely needs work.
            fresh_set.add(canonical_addr)
            continue
        has_prior = any(
            combo.startswith(f"{wallet_id}|") for combo in existing_keys
        )
        if not has_prior:
            fresh_set.add(canonical_addr)
            continue
        scored_ts = last_scored_ts.get(wallet_id, "")
        trade_ts = last_trade_ts.get(wallet_id, "")
        if trade_ts and trade_ts > scored_ts:
            material_changed_set.add(canonical_addr)
        else:
            already_scored_set.add(canonical_addr)
    return fresh_set, already_scored_set, material_changed_set


# ------------------------------------------------------------------------
# Step 5b — wallet score v1 persistence
# ------------------------------------------------------------------------


def _load_wallet_id(db: Database, canonical_addr: str) -> Optional[str]:
    """Return the persisted ``wallets.id`` UUID string for a canonical address."""
    row = db.fetchone(
        "SELECT id FROM wallets WHERE canonical_address = ?",
        (canonical_addr,),
    )
    return row["id"] if row else None


def _wallet_inputs_from_metrics(
    *,
    canonical_addr: str,
    wallet_id: str,
    metrics: dict,
) -> WalletScoreInputV1:
    """Map the legacy ``_compute_wallet_metrics`` payload into the typed V1 input.

    The legacy run-scan metrics deliberately do NOT carry every V1 specialist
    sub-field (e.g. profit_factor, max_drawdown, sample_fraction). The
    remaining fields default to ``None`` so the V1 formula returns INCOMPLETE
    honestly instead of receiving a fake 0.
    """
    return WalletScoreInputV1(
        wallet_id=wallet_id,
        info_score=None,
        win_rate=metrics.get("win_rate"),
        profit_factor=None,
        trade_intervals_std=None,
        trade_count=metrics.get("trade_count"),
        max_drawdown=None,
        sharpe_ratio=metrics.get("sharpe_ratio"),
        sample_fraction=None,
        category_trade_count=None,
        category_distinct_markets=None,
        overall_trade_count=metrics.get("trade_count"),
        largest_winner_share=None,
        top_3_concentration=None,
        # Global eligibility minimums — none are yet derivable from
        # ``_compute_wallet_metrics`` (PR 5 keeps the legacy metric path
        # untouched). Leaving them as ``None`` produces honest INCOMPLETE
        # verdicts when the formula needs them.
        resolved_markets=None,
        active_trading_days=None,
        distinct_events=None,
        category_resolved_markets=None,
        category_distinct_events=None,
        category_active_days=None,
    )


def _existing_wallet_idem_keys(
    db: Database,
    wallet_ids: Sequence[str],
    formula_version: str,
) -> set[str]:
    """Return the set of ``idempotency_key`` values already persisted for
    the given wallet IDs under the V1 formula.

    Used by the bounded Step 5b to skip wallets whose current material
    inputs already match a persisted decision (no fresh insert needed).
    The set keys are ``"wallet_id|idempotency_key"`` strings so the same
    idempotency key for two different wallets does not collide.

    Returns an empty set when ``wallet_ids`` is empty so callers do not
    emit a degenerate ``IN ()`` query.
    """
    if not wallet_ids:
        return set()
    placeholders = ",".join("?" for _ in wallet_ids)
    try:
        rows = db.fetchall(
            f"""
            SELECT wallet_id, idempotency_key
            FROM wallet_score_decisions
            WHERE formula_name = 'wallet_score'
              AND formula_version = ?
              AND wallet_id IN ({placeholders})
            """,
            (formula_version, *wallet_ids),
        )
    except Exception:  # noqa: BLE001 — defensive
        return set()
    return {f"{str(r['wallet_id'])}|{str(r['idempotency_key'])}" for r in rows}


def persist_wallet_v1_decisions(
    db: Database,
    *,
    addresses: Sequence[str],
    metrics_by_address: dict[str, dict],
    now: Optional[datetime] = None,
    counters: ScanPipelineCounters,
    max_wallet_scores: Optional[int] = None,
) -> int:
    """Compute + persist the v1 wallet score decision for every address.

    The wallet list is iterated in sorted canonical-address order so the
    corpus is consumed deterministically across runs. For each wallet the
    helper computes the material-input idempotency key and either:

      * **skips** the wallet — if a row with the same
        ``(wallet_id, formula_name, formula_version, idempotency_key)``
        already exists. The ``counters.wallet_score_decisions_reused``
        counter is incremented; no DB write happens.
      * **inserts** a new immutable row — when the wallet has no matching
        existing row AND the budget ``max_wallet_scores`` has not been
        exhausted. The ``counters.wallet_score_decisions_persisted``
        counter is incremented.
      * **defers** the wallet — when the budget is exhausted before this
        wallet was reached. The ``counters.wallet_scores_deferred``
        counter is incremented; the next scan will continue where this
        one stopped because the sorted iteration order is stable.

    This skip-already-scored progression ensures the scan makes forward
    progress through the wallet corpus even when ``max_wallet_scores``
    is much smaller than the discovered set: wallets already scored
    with the same material inputs are no-ops, so the budget is consumed
    only by fresh work and naturally rotates through previously-
    deferred wallets across successive runs.

    When ``max_wallet_scores`` is ``None`` (default), no budget is
    applied and every wallet in ``addresses`` is processed. This
    preserves the previous behavior for callers that don't want
    bounded progression (e.g. one-shot backfills, the legacy unit
    tests in ``tests/test_p04_*``).

    Returns the number of rows that took effect (inserted or reused).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Deterministic iteration order — the corpus is always consumed in
    # sorted canonical-address order so repeated scans make progress.
    sorted_addrs = sorted(set(addresses))

    # Resolve wallet_ids up front and pre-load the existing
    # (wallet_id, idempotency_key) set so the inner loop is one
    # straight-line decision per wallet.
    addr_to_wallet_id: dict[str, Optional[str]] = {}
    for canonical_addr in sorted_addrs:
        addr_to_wallet_id[canonical_addr] = _load_wallet_id(
            db, canonical_addr,
        )
    candidate_wallet_ids = [
        wid for wid in addr_to_wallet_id.values() if wid is not None
    ]
    existing_keys = _existing_wallet_idem_keys(
        db, candidate_wallet_ids, WALLET_FORMULA_VERSION,
    )

    applied = 0
    budget_remaining = max_wallet_scores
    # Wallets that received a FRESH wallet_score_decisions insert in
    # this run. Returned to the caller so Step 5e can scope its audit
    # writes to exactly the rows this run produced (skip-already-scored
    # wallets keep their existing audit rows untouched; deferred
    # wallets must not appear in this run's audit at all).
    fresh_insert_wallet_ids: list[str] = []
    for canonical_addr in sorted_addrs:
        metrics = metrics_by_address.get(canonical_addr)
        if metrics is None:
            # Defensive: caller didn't pre-compute metrics for this wallet —
            # skip rather than fabricate a result.
            continue
        wallet_id = addr_to_wallet_id.get(canonical_addr)
        if wallet_id is None:
            continue
        idem_key = _wallet_idempotency_key(canonical_addr, metrics)
        existing_combo = f"{wallet_id}|{idem_key}"
        if existing_combo in existing_keys:
            # Already persisted with identical material inputs. No-op
            # so the budget is consumed only by fresh work. Counted as
            # reused; never counted as deferred.
            counters.wallet_score_decisions_reused += 1
            continue
        if budget_remaining is not None and budget_remaining <= 0:
            # Budget exhausted; defer remaining wallets to next scan.
            counters.wallet_scores_deferred += 1
            continue
        input_obj = _wallet_inputs_from_metrics(
            canonical_addr=canonical_addr,
            wallet_id=wallet_id,
            metrics=metrics,
        )
        result = compute_wallet_score_v1(input=input_obj, now=now)
        before = _count_rows(db, "wallet_score_decisions")
        try:
            persist_wallet_score_v1(
                db,
                wallet_id,
                result,
                idempotency_key=idem_key,
                source_data_timestamp=now.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never abort scan
            logger.warning(
                "persist_wallet_score_v1 failed for %s: %s",
                canonical_addr[:12], exc,
            )
            continue
        db.conn.commit()
        after = _count_rows(db, "wallet_score_decisions")
        if after > before:
            counters.wallet_score_decisions_persisted += 1
            existing_keys.add(existing_combo)
            applied += 1
            fresh_insert_wallet_ids.append(wallet_id)
            if budget_remaining is not None:
                budget_remaining -= 1
        else:
            # UNIQUE collision — the row already existed. This is the
            # safety net for a race between this scan and another
            # writer (e.g. another timer run). Treated as a reuse.
            counters.wallet_score_decisions_reused += 1
            existing_keys.add(existing_combo)
            applied += 1
    # Stash the fresh-insert wallet IDs on the counters so the caller
    # can scope Step 5e to exactly the wallets that received a new row
    # in this run. Using a private attribute (leading underscore) keeps
    # this out of the public counters surface.
    counters._fresh_insert_wallet_ids = fresh_insert_wallet_ids
    return applied


def _canonical_metrics_blob(metrics: dict) -> str:
    """Return a stable JSON blob for the canonical subset of wallet metrics.

    Only the fields the V1 formula actually consumes are included. JSON
    keys are sorted so the blob is byte-stable across runs and across
    dict-insertion order, which is the property required for stable
    idempotency-key derivation.

    Missing values, ``None``, and ``False`` are normalized to a single
    canonical ``None`` form so two metric dicts that differ only by
    a missing-vs-None-vs-False field produce the same blob. This
    matches the review-blocker spec: "canonicalized metric payload
    actually used by the formula" — the formula treats any of those
    three as "no evidence for this field".

    Timestamps are normalized to ISO strings, floats are rounded to
    9 decimal places, and bools are normalized to ``None`` when falsy
    to keep the canonical form compact.
    """
    canonical_keys = (
        "sharpe_ratio",
        "win_rate",
        "trade_count",
        "markets_traded",
        "is_sample",
        "latest_trade_ts",
        "first_trade_ts",
    )
    out: dict[str, object] = {}
    for k in canonical_keys:
        v = metrics.get(k, None)
        # Check ``bool`` BEFORE ``int``/``float`` because in Python
        # ``isinstance(True, int) is True`` and the float branch below
        # would try to round a bool.
        if isinstance(v, bool):
            # Falsy bools collapse to None: a missing bool and an
            # explicit ``False`` are the same "no evidence" signal.
            out[k] = None if not v else True
        elif v is None:
            out[k] = None
        elif hasattr(v, "isoformat"):
            # datetime / date — normalize to ISO string so the blob is
            # comparable across runtimes without relying on Python repr.
            out[k] = v.isoformat()
        elif isinstance(v, float):
            # Round to a stable precision so float repr noise does not
            # produce new idempotency keys for the same logical value.
            out[k] = float(round(v, 9))
        else:
            out[k] = v
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def _wallet_idempotency_key(
    canonical_addr: str,
    metrics: dict,
    formula_version: str = WALLET_FORMULA_VERSION,
) -> str:
    """Deterministic per-wallet idempotency key for the V1 wallet score row.

    The key is keyed on the canonical address and a canonical hash of the
    material inputs (the metrics the formula actually consumes).
    Identical material inputs produce identical keys, so re-running the
    scan over the same inputs never duplicates semantic decisions.
    Wall-clock scan time is intentionally NOT included because the
    runtime is allowed to vary across timer invocations while the
    underlying decision identity must remain stable.

    The formula version is included so a future formula bump creates a
    new immutable decision row rather than colliding with a frozen V1 row.
    """
    blob = json.dumps(
        {
            "wallet": canonical_addr,
            "v": formula_version,
            "metrics": json.loads(_canonical_metrics_blob(metrics)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_hex(blob)[:32]


# ------------------------------------------------------------------------
# Step 5c — category wallet score v1 persistence
# ------------------------------------------------------------------------

def _category_idempotency_key(
    canonical_addr: str,
    category_label: str,
    metrics: dict,
    formula_version: str = "category_wallet_score/1",
) -> str:
    """Stable idempotency key for a (wallet, category) score decision.

    Keyed on canonical address, category label, the canonical material
    metrics the formula consumes, and the formula version. Wall-clock scan
    time is intentionally NOT included so re-running the scan with
    identical inputs is a no-op (see PR 5 charter §3).
    """
    blob = json.dumps(
        {
            "wallet": canonical_addr,
            "category": category_label,
            "v": formula_version,
            "metrics": json.loads(_canonical_metrics_blob(metrics)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_hex(blob)[:32]


def persist_category_v1_decisions(
    db: Database,
    *,
    addresses: Sequence[str],
    categories_per_wallet: dict[str, Sequence[str]],
    now: Optional[datetime] = None,
    counters: ScanPipelineCounters,
) -> int:
    """Persist a category-wallet-score v1 decision per (wallet, category).

    Categories with insufficient evidence still produce an honest INCOMPLETE
    row rather than a fake score. The frozen formula guarantees that.

    ``categories_per_wallet`` maps canonical address → sequence of category
    labels observed for that wallet. An empty sequence emits no rows.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    applied = 0
    for canonical_addr in addresses:
        wallet_id = _load_wallet_id(db, canonical_addr)
        if wallet_id is None:
            continue
        categories = categories_per_wallet.get(canonical_addr, ()) or ()
        for category_label in categories:
            if not category_label or not str(category_label).strip():
                # Empty label is honest INCOMPLETE — we never fabricate one.
                continue
            input_obj = CategoryWalletScoreInputV1(
                wallet_id=wallet_id,
                category_label=str(category_label),
                # We do NOT fabricate raw category evidence. PR 5 only has
                # legacy metrics; the gate values are honest None → the
                # formula returns INCOMPLETE without inventing evidence.
                info_score=None,
                win_rate=None,
                profit_factor=None,
                trade_intervals_std=None,
                trade_count=None,
                max_drawdown=None,
                sharpe_ratio=None,
                sample_fraction=None,
                category_trade_count=None,
                category_distinct_markets=None,
                overall_trade_count=None,
                largest_winner_share=None,
                top_3_concentration=None,
                category_resolved_markets=None,
                category_distinct_events=None,
                category_active_days=None,
                source_data_timestamp=now.isoformat(),
            )
            result = compute_category_wallet_score_v1(input=input_obj, now=now)
            # The category-score form in PR 5 is fed only fixed-None
            # specialist inputs from the legacy ``_compute_wallet_metrics``
            # path (PR 5 deliberately does not fabricate category
            # evidence). The idempotency key therefore reduces to
            # ``(canonical_addr, category_label, formula_version)`` and
            # is byte-stable across scan wall-clock changes. An empty
            # ``metrics`` dict flows through ``_canonical_metrics_blob``
            # as a stable empty-JSON blob, so two scans with the same
            # inputs produce identical keys.
            idem = _category_idempotency_key(
                canonical_addr, category_label, {},
            )
            before = _count_rows(db, "category_wallet_score_decisions")
            try:
                persist_category_score_v1(
                    db,
                    wallet_id,
                    str(category_label),
                    result,
                    idempotency_key=idem,
                    source_data_timestamp=now.isoformat(),
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "persist_category_score_v1 failed for %s/%s: %s",
                    canonical_addr[:12], category_label, exc,
                )
                continue
            db.conn.commit()
            after = _count_rows(db, "category_wallet_score_decisions")
            if after > before:
                counters.category_score_decisions_persisted += 1
            else:
                counters.category_score_decisions_reused += 1
            applied += 1
    return applied


# ------------------------------------------------------------------------
# Step 5d — copy candidates (PR-2 contract)
# ------------------------------------------------------------------------

def build_legacy_copyability_score(
    *,
    wallet_id: str,
    metrics: dict,
    now: datetime,
) -> CopyabilityScore:
    """Build a :class:`CopyabilityScore` from the legacy ``_compute_wallet_metrics`` payload.

    The legacy score is the one :func:`evaluate_source_trade_for_wallet`
    understands (it gates on ``Verdict.COPY_CANDIDATE``). We deliberately use
    the legacy contract here — refactoring it is out of scope for PR 5 and
    the V1 formula freeze forbids formula mutation.

    Components are populated from the legacy components when available, with
    DataQuality.UNKNOWN whenever the value is missing.
    """
    score = float(metrics.get("sharpe_ratio") or 0.0) * 100.0
    score = max(0.0, min(100.0, score))
    # Use the legacy ``compute_verdict`` rules (score>=70 → COPY_CANDIDATE,
    # score>=50 → WATCHLIST, <50 → SKIP, critical missing → INCOMPLETE).
    missing: list[MissingField] = []
    critical_missing_field_names = ("trade_count", "win_rate", "sharpe_ratio")
    for field_name in critical_missing_field_names:
        if metrics.get(field_name) is None:
            missing.append(
                MissingField(
                    field_name=field_name,
                    severity="critical",
                    penalty_applied=100.0 / 3.0,
                    quality_assigned=DataQuality.UNKNOWN,
                    note=f"{field_name} missing in legacy metrics",
                )
            )
    if missing:
        verdict = Verdict.INCOMPLETE
    elif score >= 70.0:
        verdict = Verdict.COPY_CANDIDATE
    elif score >= 50.0:
        verdict = Verdict.WATCHLIST
    else:
        verdict = Verdict.SKIP
    components = [
        ScoreComponent(
            name="sharpe_ratio",
            raw_score=max(0.0, min(100.0, (metrics.get("sharpe_ratio") or 0.0) / 3.0 * 100.0)),
            weight=20,
            quality=DataQuality.UNKNOWN if metrics.get("sharpe_ratio") is None else DataQuality.CALCULATED,
            formula="clamp(sharpe/3 * 100)",
            note=f"sharpe={metrics.get('sharpe_ratio')}",
        ),
        ScoreComponent(
            name="win_rate",
            raw_score=max(0.0, min(100.0, (metrics.get("win_rate") or 0.0) * 100.0)),
            weight=15,
            quality=DataQuality.UNKNOWN if metrics.get("win_rate") is None else DataQuality.CALCULATED,
            formula="win_rate * 100",
            note=f"win_rate={metrics.get('win_rate')}",
        ),
        ScoreComponent(
            name="trade_count",
            raw_score=max(0.0, min(100.0, float(metrics.get("trade_count") or 0))),
            weight=15,
            quality=DataQuality.UNKNOWN if metrics.get("trade_count") is None else DataQuality.OBSERVED,
            formula="raw_count",
            note=f"trade_count={metrics.get('trade_count')}",
        ),
    ]
    return CopyabilityScore(
        wallet_id=uuid.UUID(str(wallet_id)),
        score=round(score, 4),
        verdict=verdict,
        components=components,
        missing_fields=missing,
        formula_version="v1",
        computed_at=now,
        is_sample=bool(metrics.get("is_sample", False)),
    )


def _wallet_proxy(wallet_id: str, canonical_address: str) -> Wallet:
    """Build a Wallet-like object compatible with ``evaluate_source_trade_for_wallet``.

    The helper reads ``str(wallet.id)`` and ``wallet.address``; everything
    else is ignored. We DO NOT touch domain Wallet construction code — just
    delegate to the existing Pydantic model with minimal required fields.
    """
    return Wallet(
        id=uuid.UUID(wallet_id),
        address=canonical_address,
        label="run_scan_pr5",
        is_sample=False,
    )


def persist_copy_candidates_for_trades(
    db: Database,
    *,
    addresses: Sequence[str],
    metrics_by_address: dict[str, dict],
    trades_by_address: dict[str, list[SourceTrade]],
    now: Optional[datetime] = None,
    counters: ScanPipelineCounters,
    max_paper_candidates: int,
    max_trades_per_wallet: int,
) -> int:
    """Persist copy-candidate rows for every (wallet, attributed trade) pair.

    Total persisted rows are bounded by ``max_paper_candidates`` (across all
    wallets in this run). Trades per wallet are bounded by
    ``max_trades_per_wallet``.

    Returns the number of persisted rows (inserted OR existing via UNIQUE).
    Counters on the supplied :class:`ScanPipelineCounters` are updated in
    place for observability.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if max_paper_candidates <= 0 or max_trades_per_wallet <= 0:
        return 0
    persisted = 0
    for canonical_addr in addresses:
        if persisted >= max_paper_candidates:
            break
        metrics = metrics_by_address.get(canonical_addr)
        trades = trades_by_address.get(canonical_addr, [])
        if not metrics or not trades:
            continue
        wallet_id = _load_wallet_id(db, canonical_addr)
        if wallet_id is None:
            continue
        score = build_legacy_copyability_score(
            wallet_id=wallet_id,
            metrics=metrics,
            now=now,
        )
        wallet = _wallet_proxy(wallet_id, canonical_addr)
        for trade in trades[:max_trades_per_wallet]:
            if persisted >= max_paper_candidates:
                break
            counters.trades_scanned_for_candidates += 1
            try:
                candidate = evaluate_source_trade_for_wallet(
                    db,
                    wallet=wallet,
                    trade=trade,
                    score=score,
                    now=now,
                )
                candidate_id, _inserted = persist_copy_candidate(db, candidate)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "evaluate/persist_copy_candidate failed for %s: %s",
                    canonical_addr[:12], exc,
                )
                continue
            persisted += 1
            status_value = getattr(candidate, "status", "") or ""
            if status_value == "rejected_wallet":
                counters.copy_candidates_rejected_wallet += 1
            elif status_value.startswith("rejected"):
                counters.copy_candidates_rejected_other += 1
            else:
                counters.copy_candidates_created += 1
            # The candidate row IS the audit for every rejection that has no
            # resolvable market_id — see PR-2 contract §5. The candidate's
            # own ``status`` + ``status_reason`` + ``metrics_json`` carry
            # the bounded reason without inventing fake markets.
        db.conn.commit()
    return persisted


# ------------------------------------------------------------------------
# Step 5e — decision_verdicts + score_component_inputs audit trail
# ------------------------------------------------------------------------

def _count_rows(db: Database, table: str) -> int:
    try:
        row = db.fetchone(f"SELECT COUNT(*) AS c FROM {table}")
    except Exception:
        return 0
    return int(row["c"]) if row else 0


def _decision_verdict_family(verdict_str: str) -> str:
    """Map the wallet/category verdict enum string to its bounded family."""
    v = (verdict_str or "").lower()
    if v in ("copy_candidate", "watchlist", "skip", "incomplete"):
        return v
    return "incomplete"


def persist_decision_verdicts_and_components(
    db: Database,
    *,
    now: Optional[datetime] = None,
    counters: ScanPipelineCounters,
    max_verdicts: int = 50,
    scoped_wallet_ids: Optional[Sequence[str]] = None,
) -> None:
    """Backfill a single ``decision_verdicts`` row per wallet/category decision.

    The V1 wallet and category score persisters emit
    ``wallet_score_decisions`` / ``category_wallet_score_decisions`` but do
    NOT write the consolidated ``decision_verdicts`` audit table. To keep
    the V12 audit trail in sync without altering the existing persisters
    (which are heavily unit-tested in PR 4 / PR 17), this helper writes one
    ``decision_verdicts`` row per wallet-category combination created in
    this run and one row per category decision whose wallet was scored.

    ``scoped_wallet_ids`` narrows the audit to the wallets THIS run
    actually processed (Step 5b's bounded slice). When ``None``, the
    helper falls back to ``max_verdicts`` cap with an arbitrary latest
    rows scan — this fallback exists only for the unit-test path that
    seeds rows directly into the DB without going through run_scan.

    ``max_verdicts`` further caps the helper's work — older wallet
    decisions are unaffected when the cap is hit.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    applied = 0
    if scoped_wallet_ids is not None:
        # Deterministic, scoped path: the bounded slice from Step 5b.
        if not scoped_wallet_ids:
            wallet_rows = []
        else:
            placeholders = ",".join("?" for _ in scoped_wallet_ids)
            wallet_rows = db.fetchall(
                f"""
                SELECT id, wallet_id, final_score, verdict, source_data_timestamp
                FROM wallet_score_decisions
                WHERE wallet_id IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(scoped_wallet_ids),
            )
    else:
        # Fallback: latest N rows by id (test-only path).
        wallet_rows = db.fetchall(
            """
            SELECT id, wallet_id, final_score, verdict, source_data_timestamp
            FROM wallet_score_decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (max_verdicts,),
        )
    for r in wallet_rows:
        try:
            score = float(r["final_score"])
        except (TypeError, ValueError):
            score = 0.0
        verdict_str = r["verdict"] or "incomplete"
        try:
            db.conn.execute(
                """
                INSERT OR IGNORE INTO decision_verdicts (
                    wallet_id, formula_name, formula_version, verdict,
                    verdict_family, score, computed_at,
                    source_ref_type, source_ref_id,
                    exclusion_reasons_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["wallet_id"],
                    "wallet_score",
                    WALLET_FORMULA_VERSION,
                    verdict_str,
                    _decision_verdict_family(verdict_str),
                    score,
                    now_iso,
                    "wallet_id",
                    str(r["wallet_id"]),
                    None,
                ),
            )
            applied += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("decision_verdicts insert failed: %s", exc)
    if scoped_wallet_ids is not None:
        if not scoped_wallet_ids:
            cat_rows = []
        else:
            placeholders = ",".join("?" for _ in scoped_wallet_ids)
            cat_rows = db.fetchall(
                f"""
                SELECT id, wallet_id, category_label, final_score, verdict
                FROM category_wallet_score_decisions
                WHERE wallet_id IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(scoped_wallet_ids),
            )
    else:
        cat_rows = db.fetchall(
            """
            SELECT id, wallet_id, category_label, final_score, verdict
            FROM category_wallet_score_decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (max_verdicts,),
        )
    for r in cat_rows:
        try:
            score = float(r["final_score"])
        except (TypeError, ValueError):
            score = 0.0
        verdict_str = r["verdict"] or "incomplete"
        try:
            db.conn.execute(
                """
                INSERT OR IGNORE INTO decision_verdicts (
                    wallet_id, formula_name, formula_version, verdict,
                    verdict_family, score, computed_at,
                    source_ref_type, source_ref_id,
                    exclusion_reasons_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["wallet_id"],
                    "category_wallet_score",
                    "1",
                    verdict_str,
                    _decision_verdict_family(verdict_str),
                    score,
                    now_iso,
                    "category_label",
                    str(r["category_label"] or ""),
                    None,
                ),
            )
            applied += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("decision_verdicts category insert failed: %s", exc)
    db.conn.commit()
    counters.decision_verdicts_persisted = applied


def persist_score_component_inputs_for_wallet_decisions(
    db: Database,
    *,
    counters: ScanPipelineCounters,
    max_decisions: int = 50,
    scoped_wallet_ids: Optional[Sequence[str]] = None,
) -> None:
    """Emit ``score_component_inputs`` rows for wallet-score decisions in this run.

    The ``wallet_score_decisions.component_scores_json`` already serializes
    the components. This helper materializes them into the bounded
    ``score_component_inputs`` table so downstream audits can JOIN by
    ``(decision_ref_type, decision_ref_id)``.

    Idempotency: the helper pre-loads every
    ``(decision_ref_id, component_name)`` tuple already in the audit table
    for the current run's wallet-score decisions and skips inserts that would
    duplicate them. This keeps the helper safe to call on every scan without
    the ``score_component_inputs`` table growing on rerun — see PR 5 charter
    §3 ("identically canonical inputs must reproduce a stable identity").

    ``scoped_wallet_ids`` mirrors the ``persist_decision_verdicts_and_components``
    contract: when provided, the helper processes only the wallet-score
    decisions for those wallet IDs (Step 5b's bounded slice). When ``None``,
    the helper falls back to ``max_decisions`` cap with an arbitrary
    latest rows scan — this fallback exists only for the unit-test path.
    """
    if scoped_wallet_ids is not None:
        if not scoped_wallet_ids:
            rows = []
        else:
            placeholders = ",".join("?" for _ in scoped_wallet_ids)
            rows = db.fetchall(
                f"""
                SELECT id, component_scores_json
                FROM wallet_score_decisions
                WHERE wallet_id IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(scoped_wallet_ids),
            )
    else:
        rows = db.fetchall(
            """
            SELECT id, component_scores_json
            FROM wallet_score_decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (max_decisions,),
        )
    if not rows:
        counters.score_component_inputs_persisted = 0
        return

    # Pre-load existing (decision_ref_id, component_name) pairs for these
    # decision ids so a rerun is a no-op. Avoids unbounded growth of the
    # score_component_inputs table when the scan is invoked repeatedly with
    # identical wallet-score inputs.
    ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" for _ in ids)
    existing_pairs: set[tuple[int, str]] = set()
    try:
        existing = db.fetchall(
            f"SELECT decision_ref_id, component_name FROM score_component_inputs "
            f"WHERE decision_ref_type = 'wallet_score' AND decision_ref_id IN ({placeholders})",
            tuple(ids),
        )
    except Exception:
        existing = []
    for e in existing:
        existing_pairs.add((int(e["decision_ref_id"]), str(e["component_name"])))

    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in rows:
        try:
            components = json.loads(r["component_scores_json"] or "[]")
        except (TypeError, ValueError):
            components = []
        for comp in components:
            comp_name = str(comp.get("name", "") or "")
            if not comp_name:
                continue
            if (int(r["id"]), comp_name) in existing_pairs:
                continue
            try:
                db.conn.execute(
                    """
                    INSERT INTO score_component_inputs (
                        decision_ref_type, decision_ref_id, component_name,
                        raw_value, normalized_value, weight, quality,
                        formula, note, logged_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "wallet_score",
                        int(r["id"]),
                        comp_name,
                        None,
                        float(comp.get("raw_score")) if comp.get("raw_score") is not None else None,
                        float(comp.get("weight")) if comp.get("weight") is not None else None,
                        str(comp.get("quality", "") or "") or None,
                        str(comp.get("formula", "") or "") or None,
                        str(comp.get("note", "") or "") or None,
                        now_iso,
                    ),
                )
                written += 1
                existing_pairs.add((int(r["id"]), comp_name))
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning("score_component_inputs insert failed: %s", exc)
    db.conn.commit()
    counters.score_component_inputs_persisted = written


# ------------------------------------------------------------------------
# Tiny utilities
# ------------------------------------------------------------------------

def _sha256_hex(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canonical_addresses(discovery) -> list[str]:
    """Return canonical wallet addresses from a :class:`WalletDiscovery`.

    The discovery registry may store entries with mixed-case / padded forms.
    PR 5 always uses the canonicalized form for downstream persistence so
    every path agrees on identity.
    """
    out: list[str] = []
    for entry in discovery.list_wallets():
        addr = entry.get("address")
        if not addr:
            continue
        canonical = canonical_wallet_address(addr) or addr
        out.append(canonical)
    return out


__all__ = [
    "ScanPipelineCounters",
    "SlicedRunTelemetry",
    "resolve_bounded_wallet_slice",
    "persist_wallet_v1_decisions",
    "persist_category_v1_decisions",
    "persist_copy_candidates_for_trades",
    "persist_decision_verdicts_and_components",
    "persist_score_component_inputs_for_wallet_decisions",
    "build_legacy_copyability_score",
    "canonical_addresses",
]
