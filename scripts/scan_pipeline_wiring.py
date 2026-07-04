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
        "category_score_decisions_persisted",
        "category_score_decisions_reused",
        "copy_candidates_created",
        "copy_candidates_rejected_wallet",
        "copy_candidates_rejected_other",
        "decision_verdicts_persisted",
        "score_component_inputs_persisted",
        "trades_scanned_for_candidates",
    )

    def __init__(self) -> None:
        # PR-17 wallet-score table
        self.wallet_score_decisions_persisted: int = 0
        self.wallet_score_decisions_reused: int = 0
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

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


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


def persist_wallet_v1_decisions(
    db: Database,
    *,
    addresses: Sequence[str],
    metrics_by_address: dict[str, dict],
    now: Optional[datetime] = None,
    counters: ScanPipelineCounters,
) -> int:
    """Compute + persist the v1 wallet score decision for every address.

    Returns the number of rows that took effect (inserted or reused).
    Increments ``counters.wallet_score_decisions_persisted`` for fresh inserts
    and ``counters.wallet_score_decisions_reused`` for pre-existing rows
    matched by UNIQUE(wallet_id, formula_name, formula_version, idempotency_key).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    applied = 0
    for canonical_addr in addresses:
        metrics = metrics_by_address.get(canonical_addr)
        if metrics is None:
            # Defensive: caller didn't pre-compute metrics for this wallet —
            # skip rather than fabricate a result.
            continue
        wallet_id = _load_wallet_id(db, canonical_addr)
        if wallet_id is None:
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
                idempotency_key=_wallet_idempotency_key(
                    canonical_addr, metrics,
                ),
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
        else:
            counters.wallet_score_decisions_reused += 1
        applied += 1
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
    "persist_wallet_v1_decisions",
    "persist_category_v1_decisions",
    "persist_copy_candidates_for_trades",
    "persist_decision_verdicts_and_components",
    "persist_score_component_inputs_for_wallet_decisions",
    "build_legacy_copyability_score",
    "canonical_addresses",
]
