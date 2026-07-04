"""PR #20 — Specialist wallet metric aggregation (pure functions).

This module is the **evidence layer** for the already-planned
specialist wallet formula. It computes a conservative bundle of
per-wallet (and per-wallet, per-category) metrics from a trades
bundle plus a markets lookup. Every function here is **pure**:
no DB writes, no I/O, no global state.

Design rules (do not relax without a follow-on design review)
=============================================================

1. **No fake numbers.** If a metric cannot be computed honestly
   from the input bundle, return ``None``. Never substitute a 0.
2. **Honest metric set.** Only metrics that can be derived from
   existing schema fields (per §4 of the audit report) are
   returned. Blocked metrics (M5/M6/M7/M8) are NOT in the output
   dict — they would invite fake zeros.
3. **Conservative category handling.** Category labels are taken
   verbatim from the bundle. If the caller passes ``category_label=None``
   or empty string, the per-category subset is empty and the
   wallet-level fields are still produced.
4. **Trades are pre-filtered.** The caller is responsible for
   passing only trades attributable to the wallet via the
   canonical-address join.

Public API
==========

* :func:`compute_wallet_specialist_metrics` — entry point; returns
  the full evidence dict for one wallet (with optional category
  filter). See audit report §6 for the field list.
* :func:`aggregate_specialist_metrics` — convenience helper that
  groups trades by market and computes the evidence dict given
  a list of trade dicts and a market-id → market dict lookup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional


# ---- Trade / market shapes --------------------------------------------------

# A "trade" dict is expected to expose at least:
#   - "timestamp"  (ISO-8601 string, naive or tz-aware)
#   - "market_source_id"  (joins to markets.source_id)
#   - "is_sample"  (0/1)
# We do not import the domain dataclass here to keep this module
# unit-testable without a DB.

# A "market" dict is expected to expose at least:
#   - "id"          (UUID)
#   - "source_id"   (joins to source_trades.market_source_id)
#   - "resolved"    (0/1)
#   - "closed"      (0/1)
#   - "resolution_outcome"  (str | None)
#   - "fetched_at"  (ISO-8601 string | None)
#   - "active"      (0/1)


# ---- Helpers ----------------------------------------------------------------

def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string; return None on bad input.

    We treat naive timestamps as UTC so the day-bucket math stays
    stable across legacy rows.
    """
    if not isinstance(ts_str, str) or not ts_str.strip():
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _utc_day_bucket(ts: datetime) -> str:
    """Return the YYYY-MM-DD UTC day bucket for a datetime."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ---- Individual metric functions (pure) ------------------------------------

def compute_per_wallet_per_category_trade_count(
    trades: Iterable[dict],
) -> int:
    """M1 — total trade count for the wallet.

    Equal to ``len(trades)`` regardless of category. Provided as a
    named function so callers and tests can reason about it
    explicitly.
    """
    return sum(1 for _ in trades)


def compute_active_trading_days(trades: Iterable[dict]) -> Optional[int]:
    """M4 — count of distinct UTC day-buckets across the wallet's trades.

    Returns ``None`` for an empty trade set so callers can detect
    the missing-evidence case distinctly from a zero count.
    """
    days: set[str] = set()
    for t in trades:
        ts = _parse_ts(t.get("timestamp", ""))
        if ts is not None:
            days.add(_utc_day_bucket(ts))
    if not days:
        return None
    return len(days)


def compute_distinct_markets(trades: Iterable[dict]) -> Optional[int]:
    """M3 partial — count of distinct market_source_ids across trades.

    Distinct markets is the conservative proxy for distinct events
    in the current schema (no separate events table). Returns
    ``None`` when the wallet has zero trades.
    """
    market_ids: set[str] = set()
    for t in trades:
        mid = t.get("market_source_id")
        if isinstance(mid, str) and mid:
            market_ids.add(mid)
    if not market_ids:
        return None
    return len(market_ids)


def compute_distinct_events(trades: Iterable[dict]) -> Optional[int]:
    """M3 alias — same as :func:`compute_distinct_markets` in the
    current schema. Provided as a named function so a future events
    table can replace the implementation transparently.
    """
    return compute_distinct_markets(trades)


def compute_category_concentration(
    category_trade_count: Optional[int],
    overall_trade_count: Optional[int],
) -> Optional[float]:
    """M9 — share of wallet trades in this category.

    Returns ``None`` whenever either side is None or
    ``overall_trade_count == 0``. The ratio is in [0, 1].
    """
    if category_trade_count is None or overall_trade_count is None:
        return None
    if overall_trade_count <= 0:
        return None
    return category_trade_count / overall_trade_count


def compute_sample_reliability_score(trades: Iterable[dict]) -> Optional[float]:
    """M10 — fraction of trades that are NOT sample data.

    Range [0.0, 1.0]. Returns ``None`` when the wallet has zero
    trades. ``is_sample`` is expected to be 0 or 1; we treat any
    truthy value as sample.
    """
    total = 0
    real = 0
    for t in trades:
        total += 1
        if not t.get("is_sample"):
            real += 1
    if total == 0:
        return None
    return real / total


def compute_holding_period_days(trades: Iterable[dict]) -> Optional[int]:
    """M11 partial — span in whole days between earliest and latest
    trade on the wallet.

    Returns ``None`` when fewer than 2 parseable timestamps are
    available. This is NOT a per-trade holding period (we have no
    exit timestamp on source_trades); it is the wallet's observed
    activity span.
    """
    timestamps: list[datetime] = []
    for t in trades:
        ts = _parse_ts(t.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)
    if len(timestamps) < 2:
        return None
    span = max(timestamps) - min(timestamps)
    return max(0, span.days)


def compute_market_resolution_state(market: Optional[dict]) -> str:
    """M17 — quality-tagged market state flag.

    Returns one of:
      * ``"resolved"``  — ``market.resolved == 1`` and
        ``market.resolution_outcome`` is a non-empty string
      * ``"closed_unresolved"``  — ``market.closed == 1`` but not
        resolved
      * ``"active"``  — ``market.active == 1`` and not closed/resolved
      * ``"unknown"``  — no market row or unparseable state
    """
    if not isinstance(market, dict):
        return "unknown"
    try:
        if int(market.get("resolved") or 0) == 1 and market.get("resolution_outcome"):
            return "resolved"
        if int(market.get("closed") or 0) == 1:
            return "closed_unresolved"
        if int(market.get("active") or 0) == 1:
            return "active"
    except (TypeError, ValueError):
        return "unknown"
    return "unknown"


# ---- Top-level aggregator ---------------------------------------------------

def aggregate_specialist_metrics(
    *,
    wallet_id: str,
    category_label: Optional[str],
    all_trades_for_wallet: list[dict],
    category_trades_for_wallet: list[dict],
    now: Optional[datetime] = None,
) -> dict:
    """Compute the full specialist evidence bundle for one wallet.

    Parameters
    ----------
    wallet_id:
        The wallet UUID. Echoed back into the result under
        ``wallet_id`` so the persistence layer does not need to
        re-thread it.
    category_label:
        The category to compute per-category fields for. Pass
        ``None`` or ``""`` when no category resolves (the
        per-category fields will then be ``None`` or 0 by design).
    all_trades_for_wallet:
        Every trade attributable to this wallet via
        ``source_trades.trader_address → wallets.canonical_address``.
        Used for wallet-level metrics.
    category_trades_for_wallet:
        The subset of ``all_trades_for_wallet`` whose markets fall
        under ``category_label``. When ``category_label`` is empty
        this may be an empty list — that is the documented
        behavior (no fake category concentration).
    now:
        Optional clock for reproducibility in tests. Unused at
        present (reserved for a future "staleness" component).

    Returns
    -------
    dict
        The evidence bundle. Keys match the column list in audit
        report §6. Numeric values are ``None`` when honest
        computation is not possible; ``quality`` is one of
        ``"observed" | "partial" | "unknown" | "incomplete"``;
        ``missing_essentials_json`` is the JSON list of blocked
        metric names. **No keys are ever faked as 0.**

    Notes
    -----
    * Distinct events ≡ distinct markets in the current schema. We
      compute both so the persistence layer can persist them as
      separate columns without a follow-on migration when an events
      table arrives.
    * Blocked metrics (win_rate_realized, realized_pnl,
      profit_factor, max_drawdown) are intentionally absent. They
      go into ``missing_essentials_json``.
    """
    trade_count = len(all_trades_for_wallet)
    category_trade_count = len(category_trades_for_wallet)

    distinct_markets = compute_distinct_markets(all_trades_for_wallet)
    distinct_events = compute_distinct_events(all_trades_for_wallet)
    active_trading_days = compute_active_trading_days(all_trades_for_wallet)

    # Per-category distinct markets / active days — use the
    # category_trades subset only.
    category_distinct_markets = compute_distinct_markets(category_trades_for_wallet)
    category_active_days = compute_active_trading_days(category_trades_for_wallet)

    category_concentration = compute_category_concentration(
        category_trade_count=category_trade_count,
        overall_trade_count=trade_count,
    )
    sample_reliability = compute_sample_reliability_score(all_trades_for_wallet)
    holding_period_days = compute_holding_period_days(all_trades_for_wallet)

    missing: list[str] = []
    if trade_count == 0:
        # Truly empty wallet — every evidence item is incomplete.
        missing.append("trade_count")
    # Blocked metrics — always missing until settlement data lands.
    missing.extend([
        "resolved_markets",
        "win_rate_realized",
        "realized_pnl",
        "profit_factor",
        "max_drawdown",
    ])
    # Category fields are only meaningful when the caller provided
    # a non-empty category label.
    if not category_label:
        missing.append("category_label")
        category_trade_count = 0
        category_distinct_markets = None
        category_active_days = None
        category_concentration = None

    if "trade_count" in missing:
        quality = "incomplete"
    elif missing:
        quality = "partial"
    else:
        quality = "observed"

    component_scores_json = {
        "wallet_id": wallet_id,
        "category_label": category_label or "",
        "trade_count": trade_count,
        "distinct_markets": distinct_markets,
        "distinct_events": distinct_events,
        "active_trading_days": active_trading_days,
        "category_trade_count": category_trade_count,
        "category_distinct_markets": category_distinct_markets,
        "category_active_days": category_active_days,
        "category_concentration": category_concentration,
        "sample_reliability_score": sample_reliability,
        "holding_period_days": holding_period_days,
        # SHADOW state strings only — never a numeric value.
        "behavior_classification": "unknown",
        "copyability_evidence_state": "unknown",
        "price_improvement_state": "unknown",
        "market_resolution_state": "unknown",
    }

    return {
        "wallet_id": wallet_id,
        "category_label": category_label or "",
        # READY NOW
        "trade_count": trade_count,
        "distinct_markets": distinct_markets,
        "distinct_events": distinct_events,
        "active_trading_days": active_trading_days,
        "category_trade_count": category_trade_count,
        "category_distinct_markets": category_distinct_markets,
        "category_active_days": category_active_days,
        "category_concentration": category_concentration,
        "sample_reliability_score": sample_reliability,
        # PARTIAL
        "holding_period_days": holding_period_days,
        # SHADOW (state strings only)
        "behavior_classification": "unknown",
        "copyability_evidence_state": "unknown",
        "price_improvement_state": "unknown",
        # Quality / missing bookkeeping
        "component_scores_json": component_scores_json,
        "quality": quality,
        "missing_essentials_json": sorted(set(missing)),
    }


# ---- Group-by-category helper (used by run_scan call site) -----------------

def group_trades_by_market(
    trades: Iterable[dict],
) -> dict[str, list[dict]]:
    """Group a flat trade list by ``market_source_id``.

    Returned dict preserves insertion order of first occurrence so
    downstream consumers can iterate deterministically. Trade rows
    with missing or empty ``market_source_id`` are dropped — they
    cannot contribute to any per-market metric anyway.
    """
    grouped: dict[str, list[dict]] = {}
    for t in trades:
        mid = t.get("market_source_id")
        if not isinstance(mid, str) or not mid:
            continue
        grouped.setdefault(mid, []).append(t)
    return grouped


def group_category_labels_per_wallet(
    *,
    market_source_id_to_category: dict[str, str],
    trades: Iterable[dict],
) -> dict[str, str]:
    """Pick ONE category label per wallet from its trades.

    A wallet that trades across multiple categories gets the
    label of its **first observed market** (by iteration order —
    callers should pre-sort if determinism matters). Wallets whose
    every trade maps to a market without a category label are
    returned with an empty-string label. This is the documented
    conservative behavior: no synthetic fallback, no multi-label
    explosion in this PR.
    """
    chosen: dict[str, str] = {}
    for t in trades:
        mid = t.get("market_source_id")
        if not isinstance(mid, str) or not mid:
            continue
        addr = t.get("trader_address")
        if not isinstance(addr, str) or not addr:
            continue
        addr_key = addr.lower()
        if addr_key in chosen:
            continue
        label = market_source_id_to_category.get(mid, "")
        chosen[addr_key] = label
    return chosen