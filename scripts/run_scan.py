#!/usr/bin/env python3
"""run_scan.py — Full smart-money scan orchestrator.

Ties together all phases into a single CLI command:
1. Wallet discovery (multi-source, dedup)
2. Trade detection (staleness + dedup)
3. Copyability scoring (deterministic 0-100)
4. Verdict assignment (COPY_CANDIDATE / WATCHLIST / SKIP / INCOMPLETE)
5. Signal generation (edge-based)
6. Paper decision recording (skip for now — manual approval required)
7. Mark-to-market for any open paper positions
8. Experiment run recording
9. Missing data logging

Exit codes:
    0 — scan completed (may include partial failures)
    1 — fatal error
    2 — lock held by another process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.adapters.polymarket import (
    parse_clob_token_ids,
    zip_outcomes_with_tokens,
)
from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.db.market_persistence import persist_market_preserving_identity
from polycopy.db.wallet_identity import (
    address_column_normalized,
    canonical_wallet_address,
    is_sentinel_trader_address,
)
from polycopy.discovery.wallet_discovery import (
    RelatedWalletDetector,
    TradeDetector,
    WalletDiscovery,
)
from polycopy.domain.experiment import ExperimentRun, ExperimentStatus
from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.order import OrderSide
from polycopy.domain.source_trade import SourceTrade
from polycopy.engine.evaluate import evaluate_wallet
from polycopy.utils.concurrency import FileLock, LockError, lock_path

# Shared live-trade ingestion helper (PR #3 P2 fix). Imports are at module
# scope so both run_scan and collect_smart_money_data consume the SAME
# PolymarketPublicAdapter construction path and the SAME normalization.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _live_ingest import (  # type: ignore[import-not-found]
        PolymarketPublicAdapter,  # re-exported for type annotations
        build_trade_adapter,
        fetch_recent_trades_for_market,
    )
except ImportError:  # pragma: no cover — defensive: fall back to direct adapter import
    from polycopy.adapters.polymarket import PolymarketPublicAdapter  # type: ignore[no-redef]
    fetch_recent_trades_for_market = None  # type: ignore[assignment]
    build_trade_adapter = None  # type: ignore[assignment] 

logger = logging.getLogger(__name__)


def setup_logging(verbosity: int = 0) -> None:
    level = logging.WARNING
    if verbosity >= 1:
        level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


class ScanResult:
    """Aggregated results from a full scan.

    Wallet counters (round 11 / P3 PRRT_kwDOTG4Cf86M7Xbp):

      * ``wallets_loaded_existing`` — number of canonical wallets already
        present in the ``wallets`` table and loaded into the in-memory
        discovery registry at Step 1. Set once, never mutated.
      * ``wallets_discovered_new`` — number of NEW canonical wallets added
        to the in-memory discovery registry during this scan AND whose
        ``wallets`` row persisted successfully. A wallet whose row insert
        failed does NOT increment this counter. A repeated scan over the
        same set of wallets increments this by zero.
      * ``wallets_total_known`` — ``len(discovery.list_wallets())`` at the
        end of Step 3. Always ``loaded_existing + discovered_new`` for
        the discovery registry; downstream consumers can use this as
        "how many canonical wallets does the run know about".
      * ``wallets_discovered`` — back-compat alias. Set equal to
        ``wallets_discovered_new`` so callers that read
        ``result.wallets_discovered`` to mean "how many new wallets did
        this run find" still get the right number. The pre-round-11
        meaning ("total known in the discovery registry, including
        pre-existing") is no longer the canonical interpretation; use
        ``wallets_total_known`` for that.
    """

    def __init__(self) -> None:
        # Round 11 wallet counters (explicit, semantically distinct).
        self.wallets_loaded_existing: int = 0
        self.wallets_discovered_new: int = 0
        self.wallets_total_known: int = 0
        # Back-compat alias for callers that read .wallets_discovered
        # to mean "new wallets this run". Defined to equal new.
        self.wallets_discovered: int = 0
        self.wallets_scored: int = 0
        self.trades_total: int = 0
        # Round 7 counters — distinguish phases so we can verify the
        # north-star flow (fetch → normalize → persist → discover → score).
        self.trades_fetched: int = 0       # normalized trades returned by adapter
        self.trades_persisted: int = 0     # actually inserted into source_trades
        self.trades_attributed: int = 0    # with a real (non-sentinel) wallet
        self.anonymous_trades: int = 0     # persisted with trader_address=NULL
        self.trades_processed: int = 0
        self.trades_deduped: int = 0
        self.trades_stale: int = 0
        self.copy_candidates: int = 0
        self.watchlist: int = 0
        self.skipped: int = 0
        self.incomplete: int = 0
        self.signals: int = 0
        self.related_wallets: int = 0
        self.anonymous_trades_skipped: int = 0  # legacy alias, kept for back-compat
        # PR 5 of 6 — pipeline-wiring counters.
        # ``wallet_score_decisions_persisted`` and the surrounding fields
        # are populated by ``scripts.scan_pipeline_wiring``. They live on
        # ``ScanResult`` so the result summary surfaces how much the run
        # wrote without requiring callers to inspect the wiring module.
        self.wallet_score_decisions_persisted: int = 0
        self.wallet_score_decisions_reused: int = 0
        self.category_score_decisions_persisted: int = 0
        self.category_score_decisions_reused: int = 0
        self.copy_candidates_created: int = 0
        self.copy_candidates_rejected_wallet: int = 0
        self.copy_candidates_rejected_other: int = 0
        self.decision_verdicts_persisted: int = 0
        self.score_component_inputs_persisted: int = 0
        # PR 5 — bounded-slice telemetry.
        # ``wallet_scores_processed`` is the count of wallets whose score
        # was computed + attempted this run (Steps 5b/5c/5d). The legacy
        # counters above are populated only when a row actually took
        # effect (insert or unique-match), so they can be smaller than
        # ``wallet_scores_processed`` when helpers encounter errors.
        # ``wallet_scores_skipped`` is the count of wallets that
        # ``metrics_by_address`` discovered but were deferred to a
        # subsequent run because of ``max_wallet_scores``.
        self.wallet_scores_processed: int = 0
        self.wallet_scores_skipped: int = 0
        self.trades_scanned_for_candidates: int = 0
        # Round-10 fetch-status counters (per-market, not per-row).
        self.market_fetches_complete: int = 0
        self.market_fetches_partial: int = 0
        self.market_fetches_failed: int = 0
        self.missing_data: list[str] = []
        self.errors: list[str] = []
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None

    def summary(self) -> str:
        return (
            f"scan complete\n"
            f"  market fetches: {self.market_fetches_complete} complete, "
            f"{self.market_fetches_partial} partial, {self.market_fetches_failed} failed\n"
            f"  wallets: loaded_existing={self.wallets_loaded_existing}, "
            f"discovered_new={self.wallets_discovered_new}, "
            f"total_known={self.wallets_total_known}\n"
            f"  wallets discovered (back-compat alias for new): {self.wallets_discovered}\n"
            f"  wallets scored: {self.wallets_scored}\n"
            f"    copy_candidates: {self.copy_candidates}\n"
            f"    watchlist: {self.watchlist}\n"
            f"    skipped: {self.skipped}\n"
            f"    incomplete: {self.incomplete}\n"
            f"  trades total: {self.trades_total}\n"
            f"    fetched: {self.trades_fetched}\n"
            f"    persisted: {self.trades_persisted}\n"
            f"    attributed: {self.trades_attributed}\n"
            f"    anonymous: {self.anonymous_trades}\n"
            f"    processed: {self.trades_processed}\n"
            f"      deduped: {self.trades_deduped}\n"
            f"      stale: {self.trades_stale}\n"
            f"    sentinel/anonymous skipped (legacy): {self.anonymous_trades_skipped}\n"
            f"  related wallets: {self.related_wallets}\n"
            f"  signals generated: {self.signals}\n"
            f"  PR-5 pipeline writes:\n"
            f"    wallet_score_decisions: persisted={self.wallet_score_decisions_persisted}, "
            f"reused={self.wallet_score_decisions_reused}\n"
            f"    category_score_decisions: persisted={self.category_score_decisions_persisted}, "
            f"reused={self.category_score_decisions_reused}\n"
            f"    copy_candidates: created={self.copy_candidates_created}, "
            f"rejected_wallet={self.copy_candidates_rejected_wallet}, "
            f"rejected_other={self.copy_candidates_rejected_other}, "
            f"trades_scanned={self.trades_scanned_for_candidates}\n"
            f"    decision_verdicts: {self.decision_verdicts_persisted}\n"
            f"    score_component_inputs: {self.score_component_inputs_persisted}\n"
            f"  missing data entries: {len(self.missing_data)}\n"
            f"  errors: {len(self.errors)}"
        )


async def run_scan(
    db: Database,
    settings=None,
    market_limit: int = 20,
    use_sample: bool = False,
    *,
    max_paper_candidates: int = 25,
    max_trades_per_wallet: int = 3,
    max_wallet_scores: int = 50,
    enable_pr5_pipeline: bool = True,
) -> ScanResult:
    """Execute the full scan pipeline.

    Steps:
    1. Load wallets from DB (discovered + manual watchlist)
    2. Fetch active markets from Polymarket
    3. For each market, fetch trades → discover new wallets
    4. Run trade detection (dedup + staleness)
    5. Score all wallets (legacy path — counters only)
    5b. (PR 5) Persist v1 wallet score decisions
    5c. (PR 5) Persist v1 category wallet score decisions
    5d. (PR 5) Persist copy candidates (PR-2 contract)
    5e. (PR 5) Persist decision_verdicts + score_component_inputs audit trail
    6. Run related-wallet detection
    7. Generate paper-signal decisions for eligible candidates
    8. Record experiment run

    ``max_paper_candidates`` and ``max_trades_per_wallet`` bound the PR-5
    pipeline work so the scan runtime remains bounded. The existing PR 4
    paper-signal Step 7 continues to operate on whatever
    ``copy_candidates`` exist after Step 5d.

    ``enable_pr5_pipeline=False`` is the explicit escape hatch that
    short-circuits every PR-5 write (Steps 5b–5e). It is provided for
    test-suite scenarios that want the legacy Step 5 / Step 7 behavior
    without the new persistence writes. Production scans never set this
    to False.
    """
    if settings is None:
        settings = get_settings()

    result = ScanResult()
    discovery = WalletDiscovery()
    related_detector = RelatedWalletDetector()
    trade_detector = TradeDetector(
        staleness_seconds=settings.staleness_seconds,
        dedup_window_seconds=settings.dedup_window_seconds,
        dedup_granularity_seconds=settings.dedup_granularity_seconds,
    )

    now = datetime.now(timezone.utc)

    # ── Step 1: Load existing wallets from DB ──────────────────────────────
    logger.info("Step 1: Loading existing wallets from database...")
    # Defensive: filter sentinel / empty / whitespace-only addresses in
    # SQL (using the shared canonicalization fragment) AND in Python so a
    # row that somehow slipped past the v5 migration cleanup (e.g. an
    # upgrade interrupted before v5 finished, or rows inserted manually
    # after the upgrade) never enters the watchlist / scoring loop. The
    # SQL filter uses the SAME predicate as the v5 migration cleanup
    # AND as ``address_column_normalized`` so every path agrees.
    wallet_rows = [
        row
        for row in db.fetchall(
            f"""SELECT address, label FROM wallets
                WHERE NOT ({address_column_normalized('address')} = ''
                   OR {address_column_normalized('address')} IN ('unknown', 'anonymous', 'missing', '0x', '0x0'))"""
        )
        if not is_sentinel_trader_address(row["address"])
    ]
    for row in wallet_rows:
        canonical = canonical_wallet_address(row["address"])
        # If the DB row's address is not yet canonical (legacy mixed-case
        # or padded form), normalize before registering in the discovery
        # object so the in-memory key and the canonical SQL form agree.
        discovery.add_to_watchlist(canonical or row["address"], row["label"])
    # Round 11 (P3): snapshot the pre-existing count BEFORE Step 3 mutates
    # the discovery registry. This is the denominator for the new-vs-existing
    # counter split; pre-existing wallets must never increment the
    # "discovered_new" counter even if they re-appear during Step 3.
    result.wallets_loaded_existing = len(discovery.list_wallets())
    # Back-compat alias is set to the pre-existing count at this point;
    # at the end of Step 3 we reassign it to the new-wallet count for
    # the back-compat "wallets discovered this run" reading.
    result.wallets_discovered = 0
    logger.info("  Loaded %d existing wallets", result.wallets_loaded_existing)

    # ── Step 2: Fetch active markets ───────────────────────────────────────
    logger.info("Step 2: Fetching active markets...")
    market_list, asset_to_outcome_map = await _fetch_markets(
        db, settings, market_limit, result, use_sample,
    )
    logger.info("  Fetched %d markets", len(market_list))

    # ── Step 3: Fetch trades per market → discover wallets ────────────────
    logger.info("Step 3: Fetching trades for %d markets...", len(market_list))
    # `all_trades` retains every fetched trade (anonymous + attributed) for
    # provenance / market-level counts / persistence. Anonymous trades are
    # still persisted upstream via the ingest path; they simply don't reach
    # wallet-dependent consumers below.
    all_trades = []
    for market in market_list:
        # Per-market asset → outcome map is the same one the collector
        # uses (built from the Gamma clobTokenIds / outcomes payload).
        # Threading it through `fetch_recent_trades_for_market` →
        # `adapter.fetch_trades_for_market` → `_absorb_trade` ensures the
        # scanner rewrites a denormalized raw ``outcome`` field identically
        # to the collector BEFORE persistence, so source_trade_id,
        # market_source_id, outcome, side, etc. are byte-equal across both
        # paths for the same raw Data API row.
        asset_to_outcome = asset_to_outcome_map.get(market.source_id) or {}
        fetch_result = await _fetch_trades(
            db, market.source_id, now, result, use_sample,
            asset_to_outcome=asset_to_outcome,
        )
        # Round-10 fetch-result contract: branch on the explicit status.
        #   - "complete" → persist + discover
        #   - "partial"  → discard prefix, do NOT discover, log + counter
        #   - "failed"   → nothing to do
        if fetch_result.status == "failed":
            result.market_fetches_failed += 1
            result.missing_data.append(
                f"Market fetch FAILED for {market.source_id}: "
                f"{fetch_result.error}"
            )
            logger.warning(
                "Market %s fetch FAILED (%d rows): %s",
                market.source_id, fetch_result.rows_fetched, fetch_result.error,
            )
            continue
        if fetch_result.status == "partial":
            result.market_fetches_partial += 1
            result.missing_data.append(
                f"Market fetch PARTIAL for {market.source_id} "
                f"(pages={fetch_result.pages_fetched}, "
                f"rows={fetch_result.rows_fetched}, "
                f"error={fetch_result.error})"
            )
            logger.warning(
                "Market %s fetch PARTIAL (%d pages, %d rows): %s — "
                "prefix discarded (not persisted)",
                market.source_id, fetch_result.pages_fetched,
                fetch_result.rows_fetched, fetch_result.error,
            )
            continue
        # status == "complete"
        result.market_fetches_complete += 1
        # Round 7 (P2 fix): persist fetched trades into source_trades BEFORE
        # wallet scoring so that ``_compute_wallet_metrics`` actually sees
        # the live trade history. Anonymous and sentinel-attributed trades
        # persist with ``trader_address=None``; only attributed trades
        # become wallet rows. If persistence fails, the trade is excluded from
        # wallet discovery/scoring so we never score against missing raw history.
        persisted_trades: list[SourceTrade] = []
        for trade in fetch_result.trades:
            result.trades_fetched += 1
            persist_result = _persist_trade(db, trade)
            if persist_result is None:
                result.errors.append(
                    f"Failed to persist trade {trade.source_trade_id}; skipped wallet scoring"
                )
                continue
            if persist_result:
                result.trades_persisted += 1
            persisted_trades.append(trade)
            if is_sentinel_trader_address(trade.trader_address):
                # Anonymous or sentinel — persists as NULL, never becomes a wallet.
                result.anonymous_trades += 1
            else:
                result.trades_attributed += 1
        all_trades.extend(persisted_trades)

        # Discover wallets from attributed trades only.
        # Round 11 (P3 PRRT_kwDOTG4Cf86M7Xbp): persistence-before-discovery.
        # The wallet must be persisted to ``wallets`` first; if the insert
        # fails, the wallet MUST NOT enter the in-memory discovery registry
        # and MUST NOT be counted as a new wallet or scored. Trade
        # persistence is independent (raw market observation is still
        # allowed) — only the wallet promotion is gated.
        for trade in persisted_trades:
            # Sentinel filter: skip NULL and legacy sentinel trader_address
            # values so they never end up as wallet rows.
            if is_sentinel_trader_address(trade.trader_address):
                result.anonymous_trades_skipped += 1
                continue
            # Canonicalize before both registering in the in-memory
            # discovery object AND persisting into ``wallets`` — the two
            # MUST agree on identity for counters and for find-or-create.
            canonical_addr = canonical_wallet_address(trade.trader_address)
            if canonical_addr is None:
                # Defensive: should already have been caught above, but a
                # second guard is cheap and makes the invariant explicit.
                result.anonymous_trades_skipped += 1
                continue
            from polycopy.domain.wallet import Wallet
            wallet = Wallet(
                address=canonical_addr,
                label=f"discovered-polymarket-{canonical_addr[:8]}",
                is_sample=trade.is_sample,
            )
            # 1. Persist the wallet row FIRST (idempotent find-or-create
            #    by canonical address). The returned id is the
            #    source-of-truth signal that the wallet row is now in
            #    the DB.
            wallet_id = _persist_wallet(db, wallet)
            if wallet_id is None:
                # Persistence failed. Record the error, do NOT add the
                # wallet to the in-memory discovery registry, do NOT
                # increment the new-wallet counter, do NOT let it reach
                # the scoring loop. The trade row itself is still in
                # source_trades (raw market observation preserved).
                result.errors.append(
                    f"Wallet persist failed for {canonical_addr[:12]}; "
                    f"skipped discovery/scoring"
                )
                logger.warning(
                    "Wallet persist failed for %s; not added to discovery",
                    canonical_addr[:12],
                )
                continue
            # 2. Wallet row is in the DB. Safe to add to the in-memory
            #    discovery registry. The discovery entry's ``is_new``
            #    flag (added in round 9) is the source of truth for the
            #    new-wallet counter — no separate pre/post lookup is
            #    needed and the "wallets_discovered" alias never inflates
            #    by counting a wallet that failed to persist.
            entry = discovery.add_from_polymarket(canonical_addr)
            if entry.get("is_new", False):
                result.wallets_discovered_new += 1

    # Separate attributed trades (real wallet address) from anonymous ones.
    # Only attributed trades may enter wallet-dependent processing.
    attributed_trades = [
        t for t in all_trades if not is_sentinel_trader_address(t.trader_address)
    ]

    result.trades_total = len(all_trades)
    # Round 11 (P3): truthful wallet counters. ``wallets_total_known`` is
    # the in-memory discovery registry size after Step 3, i.e. the
    # canonical "how many wallets does this run know about" answer.
    # ``wallets_discovered`` is the back-compat alias for
    # ``wallets_discovered_new`` (per-run new-wallet count).
    result.wallets_total_known = len(discovery.list_wallets())
    result.wallets_discovered = result.wallets_discovered_new
    logger.info(
        "  Total wallets after discovery: %d (loaded_existing=%d, "
        "discovered_new=%d, total_known=%d, attributed trades: %d, "
        "anonymous: %d)",
        result.wallets_total_known,
        result.wallets_loaded_existing,
        result.wallets_discovered_new,
        result.wallets_total_known,
        len(attributed_trades),
        result.anonymous_trades_skipped,
    )

    # ── Step 4: Trade detection (dedup + staleness) ───────────────────────
    # Only attributed trades reach the detector. The detector calls
    # wallet_address.lower() inside make_dedup_key and TrackedTrade, so it
    # would crash on anonymous trades. Anonymous trades are kept in
    # `all_trades` for provenance but excluded here.
    logger.info("Step 4: Running trade detection...")
    for trade in attributed_trades:
        tracked = trade_detector.process_trade(
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            wallet_address=trade.trader_address,
            market_source_id=trade.market_source_id,
            side=trade.side.value if hasattr(trade.side, "value") else str(trade.side),
            outcome=trade.outcome,
            quantity=trade.quantity,
            price=trade.price,
            timestamp=trade.timestamp,
            now=now,
            is_sample=trade.is_sample,
        )
        result.trades_processed += 1
        if tracked.is_duplicate:
            result.trades_deduped += 1
        if tracked.is_stale:
            result.trades_stale += 1

    logger.info(
        "  Trades: %d processed, %d deduped, %d stale",
        result.trades_processed, result.trades_deduped, result.trades_stale,
    )

    # ── Step 5: Score all wallets ─────────────────────────────────────────
    # PR 5: the legacy ``evaluate_wallet`` call still tallies
    # ``result.copy_candidates`` / ``result.watchlist`` / etc. — the
    # back-compat summary counters — but the metric payload it relies on
    # is now also passed forward to the new pipeline-wiring helpers in
    # Steps 5b–5d. We collect ``metrics_by_address`` here so PR-5 writes
    # are not duplicated by re-querying the DB.
    logger.info("Step 5: Scoring %d wallets...", result.wallets_discovered)
    wallet_addresses = [w["address"] for w in discovery.list_wallets()]
    metrics_by_address: dict[str, dict] = {}
    # PR-5: trade history by canonical address, used by Step 5d to
    # generate copy candidates. Keyed on the canonical address (not the
    # raw entry["address"]) so identity agrees with discovery + SQL.
    trades_by_address: dict[str, list[SourceTrade]] = {}
    for address in wallet_addresses:
        try:
            # Gather metrics for scoring
            metrics = _compute_wallet_metrics(db, address, now)
            if metrics is None:
                result.missing_data.append(f"Cannot compute metrics for {address[:12]}")
                continue
            metrics_by_address[address] = metrics

            score_id, summary = evaluate_wallet(
                wallet_address=address,
                source="run_scan",
                sharpe_ratio=metrics.get("sharpe_ratio"),
                win_rate=metrics.get("win_rate"),
                trade_count=metrics.get("trade_count"),
                latest_trade_ts=metrics.get("latest_trade_ts"),
                first_trade_ts=metrics.get("first_trade_ts"),
                markets_traded=metrics.get("markets_traded"),
                is_sample=metrics.get("is_sample", False),
                now=now,
            )

            result.wallets_scored += 1
            # Tally verdicts
            if "copy_candidate" in summary.lower():
                result.copy_candidates += 1
            elif "watchlist" in summary.lower():
                result.watchlist += 1
            elif "incomplete" in summary.lower():
                result.incomplete += 1
            elif "skip" in summary.lower():
                result.skipped += 1

        except Exception as e:
            result.errors.append(f"Score error {address[:12]}: {e}")
            logger.warning("Failed to score wallet %s: %s", address[:12], e)

    # PR-5: build a ``trades_by_address`` map from the in-memory
    # ``attributed_trades`` already collected by Step 4. This lets the
    # pipeline-wiring Step 5d reuse the same trade history without
    # a second DB scan.
    for trade in attributed_trades:
        trader = trade.trader_address
        if not trader or is_sentinel_trader_address(trader):
            continue
        trades_by_address.setdefault(trader, []).append(trade)

    logger.info(
        "  Scored: %d copy_candidate, %d watchlist, %d skip, %d incomplete",
        result.copy_candidates, result.watchlist, result.skipped, result.incomplete,
    )

    # ── Steps 5b–5e: PR-5 — Persist PR-17/2 paper-decision evidence ───────
    # Wrapped in a single guard so the ``pr5c`` ``ScanPipelineCounters``
    # instance is always initialized before any helper runs. When
    # ``enable_pr5_pipeline`` is False (test-only escape hatch), the
    # counters remain at zero on the result and nothing is written.
    if enable_pr5_pipeline:
        from scripts.scan_pipeline_wiring import (  # local import
            ScanPipelineCounters,
            persist_category_v1_decisions,
            persist_copy_candidates_for_trades,
            persist_decision_verdicts_and_components,
            persist_score_component_inputs_for_wallet_decisions,
            persist_wallet_v1_decisions,
        )
        pr5c = ScanPipelineCounters()

        # Step 5b — wallet score v1 (BOUNDED slice).
        # ``max_wallet_scores`` caps how many wallets this run touches so a
        # timer invocation cannot process 89k+ wallets in a single run.
        # The slice is taken in deterministic wallet-id order so repeated
        # runs progress through the wallet corpus without duplicating
        # already-scored rows (UNIQUE(wallet_id, formula_name,
        # formula_version, idempotency_key) suppresses duplicates and the
        # counters differentiate "fresh insert" from "reused"). Wallets
        # beyond the slice are deferred to subsequent runs and reported in
        # ``result.wallet_scores_skipped``.
        all_wallet_addrs = list(metrics_by_address.keys())
        if len(all_wallet_addrs) > max_wallet_scores:
            # Stable ordering: sort by canonical address. This keeps
            # repeated scans consuming the corpus in the same order
            # rather than rediscovering the ordering from dict insertion
            # (which depends on upstream discovery order).
            all_wallet_addrs.sort()
            bounded_wallet_addrs = all_wallet_addrs[:max_wallet_scores]
            wallet_scores_skipped = len(all_wallet_addrs) - len(bounded_wallet_addrs)
        else:
            bounded_wallet_addrs = all_wallet_addrs
            wallet_scores_skipped = 0
        result.wallet_scores_skipped = wallet_scores_skipped
        result.wallet_scores_processed = len(bounded_wallet_addrs)

        logger.info(
            "Step 5b: Persisting v1 wallet-score decisions "
            "(processing %d of %d wallets, %d deferred to next run)...",
            len(bounded_wallet_addrs),
            len(all_wallet_addrs),
            wallet_scores_skipped,
        )
        persist_wallet_v1_decisions(
            db,
            addresses=bounded_wallet_addrs,
            metrics_by_address=metrics_by_address,
            now=now,
            counters=pr5c,
        )
        result.wallet_score_decisions_persisted = pr5c.wallet_score_decisions_persisted
        result.wallet_score_decisions_reused = pr5c.wallet_score_decisions_reused
        logger.info(
            "  wallet_score_decisions: %d persisted, %d reused",
            result.wallet_score_decisions_persisted,
            result.wallet_score_decisions_reused,
        )

        # Step 5c — category wallet score v1.
        # The legacy run-scan path does NOT yet produce per-market
        # category metadata; the helper is wired with an empty
        # ``categories_per_wallet`` map so Step 5c persists nothing
        # rather than fabricating category labels. The helper is
        # unit-tested independently so the contract surface is complete.
        # The address list is bounded to match Step 5b so Step 5e can
        # reason about the same wallet slice.
        logger.info(
            "Step 5c: Persisting v1 category-wallet-score decisions..."
        )
        categories_per_wallet: dict[str, Sequence[str]] = {}
        applied_cats = persist_category_v1_decisions(
            db,
            addresses=bounded_wallet_addrs,
            categories_per_wallet=categories_per_wallet,
            now=now,
            counters=pr5c,
        )
        result.category_score_decisions_persisted = pr5c.category_score_decisions_persisted
        result.category_score_decisions_reused = pr5c.category_score_decisions_reused
        logger.info(
            "  category_score_decisions: %d persisted, %d reused (helpers=%d)",
            result.category_score_decisions_persisted,
            result.category_score_decisions_reused,
            applied_cats,
        )

        # Step 5d — copy candidates (PR-2 contract)
        logger.info(
            "Step 5d: Persisting copy candidates (max %d, max_trades/wallet=%d)...",
            max_paper_candidates, max_trades_per_wallet,
        )
        persist_copy_candidates_for_trades(
            db,
            addresses=bounded_wallet_addrs,
            metrics_by_address=metrics_by_address,
            trades_by_address=trades_by_address,
            now=now,
            counters=pr5c,
            max_paper_candidates=max_paper_candidates,
            max_trades_per_wallet=max_trades_per_wallet,
        )
        result.copy_candidates_created = pr5c.copy_candidates_created
        result.copy_candidates_rejected_wallet = pr5c.copy_candidates_rejected_wallet
        result.copy_candidates_rejected_other = pr5c.copy_candidates_rejected_other
        result.trades_scanned_for_candidates = pr5c.trades_scanned_for_candidates
        logger.info(
            "  copy_candidates: created=%d, rejected_wallet=%d, rejected_other=%d, "
            "trades_scanned=%d",
            result.copy_candidates_created,
            result.copy_candidates_rejected_wallet,
            result.copy_candidates_rejected_other,
            result.trades_scanned_for_candidates,
        )

        # Step 5e — decision_verdicts + score_component_inputs audit trail.
        # IMPORTANT: both helpers are scoped to the BOUNDED Step 5b/5c
        # slice so decision_verdicts and score_component_inputs only
        # reflect wallets this run actually processed (not arbitrary
        # latest rows from the whole wallet_score_decisions table). The
        # cap ``max_wallet_scores`` is passed through so the LIMIT in
        # those helpers never exceeds this run's processed slice.
        logger.info(
            "Step 5e: Persisting decision_verdicts + score_component_inputs "
            "(bounded to %d wallets processed this run)...",
            len(bounded_wallet_addrs),
        )
        persist_decision_verdicts_and_components(
            db,
            now=now,
            counters=pr5c,
            scoped_wallet_ids=(
                _resolve_wallet_ids(db, bounded_wallet_addrs)
            ),
        )
        persist_score_component_inputs_for_wallet_decisions(
            db,
            counters=pr5c,
            scoped_wallet_ids=(
                _resolve_wallet_ids(db, bounded_wallet_addrs)
            ),
        )
        result.decision_verdicts_persisted = pr5c.decision_verdicts_persisted
        result.score_component_inputs_persisted = pr5c.score_component_inputs_persisted
        logger.info(
            "  decision_verdicts: %d, score_component_inputs: %d",
            result.decision_verdicts_persisted,
            result.score_component_inputs_persisted,
        )


    # ── Step 6: Related-wallet detection ───────────────────────────────────
    logger.info("Step 6: Running related-wallet detection...")
    if len(wallet_addresses) >= 2:
        # Use first wallet as primary, check others against it
        primary = wallet_addresses[0]
        candidates = [(addr, ["shared_market"]) for addr in wallet_addresses[1:5]]
        related = related_detector.batch_evaluate(primary, candidates)
        result.related_wallets = len(related)
        logger.info("  Found %d possibly related wallets", result.related_wallets)

    # ── Step 7: Generate paper-signal decisions for eligible candidates ──
    # PR 4 (Chunk 4): replace the legacy edge-based signal generator with
    # the persisted-evidence paper-signal pipeline. This step consumes:
    #   - copy_candidates (status=READY_FOR_PAPER_SIGNAL or
    #     PENDING_PRICE_CHECK)
    #   - source_trades
    #   - candidate_price_snapshots + candidate_price_snapshot_levels
    #   - wallet_score_decisions
    #   - category_wallet_score_decisions
    #   - behavior evidence derived from source_trades
    # It must NEVER write to: orders, positions, fills, broker requests,
    # CLOB fetches, or HTTP. All paper_signal_decisions are persisted with
    # is_approved = 0 and remain unapproved at all times.
    logger.info(
        "Step 7: Evaluating paper-signal decisions for copy candidates..."
    )
    paper_signals = _evaluate_paper_signals_step(db, now=now)
    result.signals = paper_signals
    logger.info("  Paper-signal decisions recorded: %d", result.signals)

    # ── Step 8: Record experiment run ─────────────────────────────────────
    result.ended_at = datetime.now(timezone.utc)
    _record_experiment(db, result, settings)

    return result


def _compute_wallet_metrics(
    db: Database,
    address: str,
    now: datetime,
) -> dict | None:
    """Compute scoring metrics for a wallet from its trades in DB.

    Canonicalization invariant: ``address`` is matched case-insensitively
    and against ANY surrounding ASCII whitespace (tab, LF, CR, VT, FF,
    NUL, space) via the shared ``address_column_normalized`` SQL fragment
    defined in :mod:`polycopy.db.wallet_identity`. A freshly-discovered
    lowercase wallet will find trades persisted under any case variant
    AND any whitespace-padded legacy variant of the same address.

    Returns ``None`` for sentinel / empty / whitespace-only inputs so
    they can never enter scoring.
    """
    canonical = canonical_wallet_address(address)
    if canonical is None:
        return None
    trades = db.fetchall(
        f"""SELECT * FROM source_trades
           WHERE {address_column_normalized('trader_address')} = ?
             AND trader_address IS NOT NULL
           ORDER BY timestamp DESC""",
        (canonical,),
    )
    if not trades:
        return None

    trade_count = len(trades)
    is_sample = all(t["is_sample"] for t in trades)

    # Compute win rate (simplified: profitable if price moved in favor)
    # Without resolution data, we estimate based on trade side and price
    wins = 0
    for t in trades:
        # Simplified heuristic: buy trades with price < 0.5 are "value buys"
        side = t["side"]
        price = t["price"]
        if isinstance(side, str):
            side_val = side
        else:
            side_val = str(side)
        if side_val == "buy" and price < 0.5:
            wins += 1
        elif side_val == "sell" and price > 0.5:
            wins += 1

    win_rate = wins / trade_count if trade_count > 0 else None

    # Timestamps
    timestamps = []
    for t in trades:
        ts_str = t["timestamp"]
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                timestamps.append(ts)
            except (ValueError, TypeError):
                pass

    latest_trade_ts = max(timestamps) if timestamps else None
    first_trade_ts = min(timestamps) if timestamps else None

    # Markets traded
    market_ids = set(t["market_source_id"] for t in trades)
    markets_traded = len(market_ids)

    # Sharpe ratio estimate (simplified)
    sharpe_ratio = None
    if trade_count >= 5 and win_rate is not None:
        # Rough estimate: win_rate * sqrt(trade_count) * 0.5
        import math
        sharpe_ratio = round(win_rate * math.sqrt(trade_count) * 0.5, 3)

    return {
        "sharpe_ratio": sharpe_ratio,
        "win_rate": win_rate,
        "trade_count": trade_count,
        "latest_trade_ts": latest_trade_ts,
        "first_trade_ts": first_trade_ts,
        "markets_traded": markets_traded,
        "is_sample": is_sample,
    }


def _resolve_wallet_ids(
    db: Database,
    canonical_addresses: list[str],
) -> list[str]:
    """Return ``wallets.id`` UUID strings for the given canonical addresses.

    Mirrors :func:`scripts.scan_pipeline_wiring._load_wallet_id` so the
    Step 5e audit helpers can scope their writes to the bounded Step 5b
    wallet slice without re-deriving IDs. Missing wallets (e.g. an
    address discovered in Step 5 but not yet persisted) are silently
    dropped from the returned list — the helper callers only operate on
    IDs that exist, and any missing wallets simply produce zero
    decision_verdicts / score_component_inputs rows for them, which is
    safe because no upstream row was created either.
    """
    if not canonical_addresses:
        return []
    placeholders = ",".join("?" for _ in canonical_addresses)
    try:
        rows = db.fetchall(
            f"SELECT id, canonical_address FROM wallets "
            f"WHERE canonical_address IN ({placeholders})",
            tuple(canonical_addresses),
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [str(r["id"]) for r in rows]


def _generate_signals(db: Database, markets: list[Market], now: datetime) -> list[dict]:
    """LEGACY signal generator — DEPRECATED, DO NOT CALL.

    Replaced by Step 7's ``_evaluate_paper_signals_step`` (PR 4 Chunk 4).
    This stub remains only as a monkeypatch surface for the historical
    test suite (``tests/test_p22`` … ``tests/test_p36``). The function
    is NOT invoked from the live ``run_scan`` pipeline; it returns an
    empty list and performs no side effects. New code MUST use
    :func:`_evaluate_paper_signals_step` instead.
    """
    logger.debug(
        "_generate_signals (legacy) called — returning []. Use "
        "_evaluate_paper_signals_step instead."
    )
    return []


def _evaluate_paper_signals_step(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Step 7 entrypoint for the PR 4 paper-signal pipeline.

    Iterates every persisted ``copy_candidates`` row in an eligible
    status (PENDING_PRICE_CHECK or READY_FOR_PAPER_SIGNAL) and runs the
    full pipeline:

        1. Load persisted candidate + source_trade + snapshot + depth.
        2. Load wallet score v1 decision (point-in-time).
        3. Load exact category score v1 decision (point-in-time).
        4. Classify wallet behavior from persisted source_trades.
        5. Walk persisted depth levels for the snapshot.
        6. Build a typed TradeCopyabilityInputV1 from persisted truth.
        7. Compute trade copyability v1.
        8. Generate final paper-signal verdict.
        9. Persist immutable paper_signal_decisions row (is_approved=0).
       10. Register seven exit experiments (if COPY_CANDIDATE).

    All inputs are persisted. No network calls, no CLOB fetches, no
    broker/order/position writes. On missing evidence the signal is
    persisted as INCOMPLETE with the specific reason.

    Returns the number of immutable paper_signal_decisions rows
    recorded by this run.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Import inside the function so scripts that import run_scan do not
    # pay the paper-signal module import cost on cold start.
    from polycopy.scoring.paper_signal import evaluate_paper_signal_for_candidate

    total = 0

    # Iterate every persisted candidate in an eligible status. The
    # status check uses the bounded CandidateStatus enum values
    # ("PENDING_PRICE_CHECK", "READY_FOR_PAPER_SIGNAL") plus the
    # legacy "pending" string (kept for back-compat). Any candidate
    # not in an eligible status is silently skipped — it will be
    # re-evaluated when its status advances.
    candidate_rows = db.fetchall(
        """SELECT id FROM copy_candidates
           WHERE status IN ('PENDING_PRICE_CHECK', 'READY_FOR_PAPER_SIGNAL', 'pending')
           ORDER BY id ASC"""
    )

    for cand_row in candidate_rows:
        candidate_id = int(cand_row["id"])
        try:
            outcome_kind = evaluate_paper_signal_for_candidate(
                db,
                candidate_id=candidate_id,
                now=now,
            )
            if outcome_kind == "persisted":
                total += 1
        except Exception as exc:  # defensive: never abort the run
            logger.warning(
                "Paper-signal evaluation failed for candidate %d: %s",
                candidate_id, exc,
            )

    db.conn.commit()
    return total


def _persist_wallet(db: Database, wallet) -> str | None:
    """Persist a wallet row, idempotent find-or-create by canonical address.

    Steps:
      1. Compute canonical address using the single-source-of-truth helper
         ``canonical_wallet_address`` so discovery and the database agree.
      2. If canonical is None (sentinel / empty / whitespace-only), return None
         to indicate the address is anonymous and should never become a wallet row.
      3. Look up an existing row by ``canonical_address`` (new v6 column) — no
         need for the historic ``address_column_normalized('address')`` predicate
         because the new schema guarantees :column:`wallets.canonical_address`
         already contains the normalized form for *all* non-sentinel wallets.
      4. If found, return its id (no-op write, no duplicate row created).
      5. Otherwise, insert a new row using a fresh UUID, the canonical address,
         and the new canonical_address column. The ON CONFLICT clause
         provides defensive conflict handling against concurrent inserts for
         the same canonical address — a writer race still leaves a single row.
      6. Return the inserted-or-updated row's id.

    This is the v6-fix implementation that finally uses the persisted
    ``canonical_address`` column as the canonical identity source.
    """
    try:
        canonical = canonical_wallet_address(wallet.address)
        if canonical is None:
            # Anonymous / sentinel trader address — never an attributable
            # wallet row. Return None so the caller's counters do not
            # treat this as a newly-discovered wallet.
            return None

        existing = db.fetchone(
            "SELECT id FROM wallets WHERE canonical_address = ?",
            (canonical,),
        )
        if existing is not None:
            return existing["id"]

        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO wallets
               (id, address, canonical_address, label, is_sample, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(canonical_address) DO UPDATE SET
                 label = excluded.label,
                 is_sample = CASE WHEN excluded.is_sample = 0 AND is_sample = 1
                                  THEN 0
                                  ELSE is_sample END
               RETURNING id""",
            (
                new_id,
                canonical,
                canonical,
                wallet.label,
                int(wallet.is_sample),
                now,
            ),
        )
        # Re-fetch the id: if a concurrent writer beat us to the same
        # canonical address, the ON CONFLICT DO UPDATE will have updated
        # the existing row and returned its id.
        row = db.fetchone("SELECT id FROM wallets WHERE canonical_address = ?", (canonical,))
        db.conn.commit()
        return row["id"] if row else new_id
    except Exception as e:
        logger.warning("Wallet persist skipped for %r: %s", wallet.address, e)
        try:
            db.conn.rollback()
        except Exception:
            pass
        return None


async def _fetch_markets(
    db, settings, limit, result, use_sample
) -> tuple[list[Market], dict[str, dict[str, str]]]:
    """Fetch active markets from Polymarket or use sample data.

    Returns ``(markets, asset_to_outcome_map)`` where
    ``asset_to_outcome_map`` maps ``market.source_id`` → a
    ``{token_id: outcome_label}`` dict built from the same Gamma
    ``clobTokenIds`` / ``outcomes`` payload the parser consumes. This
    map is the input to the ``asset_to_outcome`` parameter threaded
    through ``fetch_recent_trades_for_market`` so the scanner rewrites
    a denormalized ``outcome`` field identically to the collector.

    For sample markets the map is empty (sample trades already carry
    correct outcome labels), so the parser falls back to the raw field.
    """
    if use_sample:
        markets = _get_sample_markets()
        for market in markets:
            _persist_market(db, market)
        return markets, {}

    import httpx
    asset_map: dict[str, dict[str, str]] = {}
    async with httpx.AsyncClient(base_url=settings.gamma_base_url, timeout=settings.http_timeout_seconds) as client:
        try:
            resp = await client.get("/markets", params={
                "active": "true", "closed": "false", "limit": limit,
                "order": "volume24hr", "ascending": "false",
            })
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                data = [data]

            markets = []
            for item in data:
                try:
                    market = _parse_gamma_market(item)
                    _persist_market(db, market)
                    # Build the asset-to-outcome map from the same raw
                    # Gamma payload so a single market fetch serves
                    # both the persistence path AND the trade-normalization
                    # path. Reuses the same JSON-decode logic the
                    # collector uses in PolymarketCollector.
                    asset_map[market.source_id] = _build_asset_to_outcome_map(item)
                    markets.append(market)
                except Exception as e:
                    result.errors.append(f"Market parse error: {e}")
            return markets, asset_map
        except Exception as e:
            result.errors.append(f"Market fetch failed: {e}")
            logger.warning(
                "Market fetch failed; returning no live markets. "
                "Sample markets are only used with --use-sample: %s",
                e,
            )
            return [], {}


async def _fetch_trades(
    db, market_source_id, now, result, use_sample,
    *, asset_to_outcome: dict[str, str] | None = None,
):
    """Fetch trades for a market or return sample trades.

    P2 fix (PR #3): live ``use_sample=False`` mode used to hit a legacy
    ``settings.gamma_base_url + /trades`` endpoint, which has never existed
    on Gamma (returns 404) and which silently fabricated ``polymarket_clob``
    trades through a local legacy parser. The actual public,
    unauthenticated trade source is the data-api
    (``data-api.polymarket.com/trades``), wired through the shared
    :class:`PolymarketPublicAdapter`. We now route BOTH ``run_scan`` and
    ``collect_smart_money_data`` through the same adapter so the
    normalization and snapshot provenance are identical.

    Behavior contract:
      - ``use_sample=True`` → returns the existing labeled sample trades
        unchanged (no adapter call). The caller still needs to see them
        as ``complete`` for counter purposes; we wrap them as a
        :class:`MarketTradeFetchResult` with status="complete" so the
        sample path participates in the same accounting.
      - ``use_sample=False`` → uses the shared adapter and returns a
        :class:`MarketTradeFetchResult` whose ``status`` is
        ``"complete"`` / ``"partial"`` / ``"failed"``. The caller MUST
        branch on status before persisting or scoring.
      - Round 7: live fetches go through ``adapter.fetch_trades_for_market``
        which uses ``GET /trades?market=<conditionId>&takerOnly=false``
        (server-side filter, bounded pagination, dedup across pages).
    """
    if use_sample:
        # Wrap the legacy list-return in the new contract so the
        # caller can use one code path for both branches.
        from polycopy.adapters.polymarket import MarketTradeFetchResult
        sample = _get_sample_trades(market_source_id)
        return MarketTradeFetchResult(
            trades=sample,
            status="complete",
            pages_fetched=1 if sample else 0,
            rows_fetched=len(sample),
            market_source_id=market_source_id,
        )

    adapter = _get_scan_trade_adapter()
    # Pass epoch-zero as ``since`` so the adapter returns the FULL per-market
    # history the API can serve (the data-api hard-caps the per-market
    # response at ``max_rows``). A scan run wants the complete recent
    # picture, not a per-call delta.
    # ``asset_to_outcome`` is threaded through from run_scan → here → the
    # shared adapter. Same map the collector uses, so scanner and collector
    # rewrite a denormalized ``outcome`` field identically.
    return await fetch_recent_trades_for_market(
        adapter,
        market_source_id=market_source_id,
        since=datetime.fromtimestamp(0, tz=timezone.utc),
        limit=200,
        asset_to_outcome=asset_to_outcome or {},
    )


def _persist_trade(db: Database, trade: SourceTrade) -> bool | None:
    """Persist one ``SourceTrade`` into ``source_trades``.

    Round 7 (P2 fix): live scan must persist fetched trades BEFORE
    ``_compute_wallet_metrics`` runs, otherwise every newly discovered
    wallet will be marked missing-data. Uses ``INSERT OR IGNORE`` against
    the ``UNIQUE(source, source_trade_id)`` index so an exact rerun is
    idempotent and distinct same-transaction rows (encoded via
    ``deterministic_source_trade_id_v2``) both persist.

    Round-8 fix: defensively normalize ``trade.trader_address`` to
    lowercase at the persistence boundary so even callers that bypass
    the adapter's parser (legacy code paths, tests, future ingest
    adapters) land the canonical form in ``source_trades``. Sentinels
    and ``None`` pass through unchanged. Combined with the parser
    lowercasing and the case-insensitive metric query, this guarantees
    canonical identity from ingestion through scoring.

    Returns True if a new row was inserted, False if the row already
    existed (idempotent retry), and None if the insertion failed. Callers
    MUST NOT score wallets for trades that return None because the raw
    trade history is not available to ``_compute_wallet_metrics``.
    """
    try:
        # Defensive normalization: None / sentinel pass through; legitimate
        # addresses are stored in canonical lowercase form. This mirrors
        # what the parser now does and keeps every persistence path
        # consistent.
        ta = trade.trader_address
        if ta is not None and ta and not is_sentinel_trader_address(ta):
            persisted_trader_address: str | None = str(ta).strip().lower() or None
        else:
            persisted_trader_address = None
        cur = db.execute(
            """INSERT OR IGNORE INTO source_trades
               (id, source, source_trade_id, market_source_id, side,
                outcome, quantity, price, trader_address, timestamp,
                is_sample, token_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                trade.source,
                trade.source_trade_id,
                trade.market_source_id,
                trade.side.value if hasattr(trade.side, "value") else str(trade.side),
                trade.outcome,
                float(trade.quantity),
                float(trade.price),
                persisted_trader_address,
                trade.timestamp.isoformat() if trade.timestamp else None,
                int(bool(trade.is_sample)),
                # PR-1: persist upstream CLOB token id verbatim. None when
                # the source payload didn't carry an asset field (legacy
                # fallback path in resolve_trade_to_outcome).
                trade.token_id,
            ),
        )
        db.conn.commit()
        # rowcount == 1 means a fresh insert; 0 means duplicate (UNIQUE hit)
        return bool(getattr(cur, "rowcount", 0))
    except Exception as e:
        logger.warning(
            "persist_trade failed (%s @ %s): %s",
            trade.source_trade_id, trade.market_source_id, e,
        )
        try:
            db.conn.rollback()
        except Exception:
            pass
        return None


# ── Shared adapter wiring (PR #3 P2) ───────────────────────────────────────
#
# ``run_scan`` and ``collect_smart_money_data`` MUST go through the SAME
# PolymarketPublicAdapter construction path so they share settings, cache,
# and normalization. ``build_trade_adapter`` is imported at module scope
# above (see the ``try: from _live_ingest import build_trade_adapter`` block
# near the top of this file). We reuse that single import below so the
# module stays runnable via direct absolute-path execution
# (``python scripts/run_scan.py`` from any cwd, no PYTHONPATH) AND via
# package-style execution (``python -m scripts.run_scan``).
#
# IMPORTANT: do NOT add a second ``from scripts._live_ingest import …``
# here. That package-style import requires ``scripts`` to be importable as
# a package, which is only true when running ``python -m scripts.run_scan``
# from the repo root — it fails for the direct-execution CLI startup path
# with ``ModuleNotFoundError: No module named 'scripts'`` and for the
# mocked live path the smoke test uses.
_SCAN_TRADE_ADAPTER: "PolymarketPublicAdapter | None" = None


def _get_scan_trade_adapter() -> "PolymarketPublicAdapter":
    """Return the process-wide shared PolymarketPublicAdapter for run_scan.

    Lazily constructs it on first use and reuses the same instance for the
    rest of the process so all per-market ``_fetch_trades`` calls share the
    same adapter configuration, parsing, throttling, and snapshot behavior.

    Uses the module-scoped ``build_trade_adapter`` import — never re-imports
    under a package-style name. See the comment above the constant.
    """
    global _SCAN_TRADE_ADAPTER
    if _SCAN_TRADE_ADAPTER is None:
        if build_trade_adapter is None:  # pragma: no cover — defensive
            # The module-scope ``try: from _live_ingest import …`` block fell
            # back to a stub because scripts/ wasn't on sys.path. That only
            # happens if someone has stripped ``sys.path`` to an extreme;
            # we still refuse to crash here and surface a clear error.
            raise RuntimeError(
                "scripts/_live_ingest.build_trade_adapter is unavailable; "
                "scripts/ must be importable to use the live trade path"
            )
        _SCAN_TRADE_ADAPTER = build_trade_adapter(get_settings())
    return _SCAN_TRADE_ADAPTER


def _build_asset_to_outcome_map(data: dict) -> dict[str, str]:
    """Build asset_id → outcome-label map for a Gamma market object.

    Mirror of
    :meth:`scripts.collect_smart_money_data.PolymarketCollector._build_asset_to_outcome_map`
    so the scanner and the collector rewrite a denormalized raw ``outcome``
    field identically for the same raw Gamma payload. The two functions
    must stay in sync — they are the single source of truth for
    ``{clobTokenId: outcomes_label}`` mapping used to fix the raw
    data-api ``outcome`` string.

    Gamma's ``clobTokenIds`` is a JSON-encoded array of token IDs in the
    same order as the ``outcomes`` array. The two are zipped position-wise.
    """
    import json as _json
    try:
        outcomes = data.get("outcomes", "[]")
        tokens = data.get("clobTokenIds", "[]")
        if isinstance(outcomes, str):
            outcomes = _json.loads(outcomes)
        if isinstance(tokens, str):
            tokens = _json.loads(tokens)
        if not isinstance(outcomes, list) or not isinstance(tokens, list):
            return {}
        return {str(tok): str(lab) for tok, lab in zip(tokens, outcomes)}
    except Exception:
        return {}


def _parse_gamma_market(data: dict) -> Market:
    """Parse a Gamma API market payload into our :class:`Market` domain model.

    PR-1: outcomes are zipped with the positional ``clobTokenIds`` array via
    the SAME shared helpers used by every other Gamma parser in the codebase
    (``PolyymarketPublicAdapter._parse_gamma_market``,
    ``scripts.collect_smart_money_data.PolymarketCollector._parse_market``,
    ``scripts.update_paper_portfolio``). Missing / malformed / length-mismatched
    token arrays produce ``clob_token_id=None`` for every outcome (INCOMPLETE),
    never a silent positional mapping. The shared helper also emits a
    structured warning that shows up in the run-scan logs.
    """
    import json as _json
    outcomes_raw = data.get("outcomes", "[]")
    prices_raw = data.get("outcomePrices", "[]")
    if isinstance(outcomes_raw, str):
        outcomes_raw = _json.loads(outcomes_raw)
    if isinstance(prices_raw, str):
        prices_raw = _json.loads(prices_raw)

    # PR-1: shared helpers — single source of truth for clob-token pairing.
    tokens = parse_clob_token_ids(data)
    zipped = zip_outcomes_with_tokens(
        outcomes_raw, tokens, source_label="run_scan._parse_gamma_market"
    )
    token_by_index: dict[int, Optional[str]] = {
        idx: tok for idx, _, tok in zipped
    }
    outcomes = []
    for i, label in enumerate(outcomes_raw):
        price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
        outcomes.append(
            MarketOutcome(
                label=str(label),
                price=price,
                clob_token_id=token_by_index.get(i),
            )
        )

    return Market(
        source_id=data.get("conditionId", data.get("id", "")),
        question=data.get("question", ""),
        outcomes=outcomes,
        source="polymarket",
        active=data.get("active", False),
        closed=data.get("closed", False),
        resolved=data.get("resolved", False),
        resolution_outcome=data.get("resolutionOutcome"),
        volume_24h=float(data.get("volume24hr", 0) or 0),
        fetched_at=datetime.now(timezone.utc),
        is_sample=False,
    )


def _persist_market(db: Database, market: Market) -> None:
    try:
        persist_market_preserving_identity(db, market)
    except Exception as e:
        logger.debug("Market persist skipped: %s", e)


def _get_sample_markets() -> list[Market]:
    """Return labeled sample markets for testing."""
    now = datetime.now(timezone.utc)
    return [
        Market(
            source_id="sample-market-001",
            question="Will Trump win 2028 election?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.72, volume=150000.0),
                MarketOutcome(label="No", price=0.28, volume=80000.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=230000.0,
            fetched_at=now, is_sample=True,
        ),
        Market(
            source_id="sample-market-002",
            question="Will BTC exceed $150k by end of 2026?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.45, volume=90000.0),
                MarketOutcome(label="No", price=0.55, volume=70000.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=160000.0,
            fetched_at=now, is_sample=True,
        ),
    ]


def _get_sample_trades(market_source_id: str) -> list[SourceTrade]:
    """Return labeled sample trades for testing."""
    now = datetime.now(timezone.utc)
    return [
        SourceTrade(
            source="sample",
            source_trade_id=f"sample-trade-{market_source_id}-001",
            market_source_id=market_source_id,
            side=OrderSide.BUY,
            outcome="Yes",
            quantity=50.0,
            price=0.72,
            trader_address="0xSAMPLE_TRADER_A_DO_NOT_USE",
            timestamp=now, is_sample=True,
        ),
        SourceTrade(
            source="sample",
            source_trade_id=f"sample-trade-{market_source_id}-002",
            market_source_id=market_source_id,
            side=OrderSide.BUY,
            outcome="Yes",
            quantity=30.0,
            price=0.70,
            trader_address="0xSAMPLE_TRADER_B_DO_NOT_USE",
            timestamp=now, is_sample=True,
        ),
    ]


def _record_experiment(db: Database, result: ScanResult, settings) -> None:
    """Record the scan as an experiment run."""
    run = ExperimentRun(
        label=f"scan-{result.started_at.strftime('%Y%m%dT%H%M%S')}",
        strategy_config={
            "script": "run_scan.py",
            "market_limit": settings.http_rate_limit_rps if hasattr(settings, "http_rate_limit_rps") else 2.0,
            "staleness_seconds": settings.staleness_seconds,
        },
        status=ExperimentStatus.COMPLETED,
        started_at=result.started_at,
        ended_at=result.ended_at,
        result_summary={
            "wallets_discovered": result.wallets_discovered,
            "wallets_scored": result.wallets_scored,
            "copy_candidates": result.copy_candidates,
            "watchlist": result.watchlist,
            "skipped": result.skipped,
            "incomplete": result.incomplete,
            "trades_total": result.trades_total,
            "trades_processed": result.trades_processed,
            "trades_deduped": result.trades_deduped,
            "trades_stale": result.trades_stale,
            "anonymous_trades_skipped": result.anonymous_trades_skipped,
            "signals": result.signals,
            "related_wallets": result.related_wallets,
            "errors": len(result.errors),
        },
        is_sample=False,
    )
    try:
        db.execute(
            """INSERT INTO experiment_runs
               (id, label, strategy_config, status, started_at, ended_at,
                result_summary, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(run.id), run.label, json.dumps(run.strategy_config),
                run.status.value,
                run.started_at.isoformat() if run.started_at else None,
                run.ended_at.isoformat() if run.ended_at else None,
                json.dumps(run.result_summary), int(run.is_sample),
            ),
        )
        db.conn.commit()
    except Exception as e:
        logger.warning("Failed to record experiment: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full smart-money scan")
    parser.add_argument("--market-limit", type=int, default=20, help="Max markets to scan")
    parser.add_argument(
        "--max-paper-candidates",
        type=int,
        default=25,
        help="PR 5: cap on persisted copy-candidate rows per scan run. "
        "Used to keep scan runtime bounded; passed to "
        "scripts.scan_pipeline_wiring.persist_copy_candidates_for_trades.",
    )
    parser.add_argument(
        "--max-trades-per-wallet",
        type=int,
        default=3,
        help="PR 5: cap on trades considered per wallet when generating "
        "copy candidates. Forward-going evidence only.",
    )
    parser.add_argument(
        "--max-wallet-scores",
        type=int,
        default=50,
        help="PR 5: cap on wallets persisted to wallet_score_decisions per "
        "scan run (Step 5b). Bounds the DB-write fan-out so the scan "
        "remains runtime-bounded even when discovery returns many wallets. "
        "Step 5e mirrors this cap so decision_verdicts and "
        "score_component_inputs only reflect the bounded slice.",
    )
    parser.add_argument(
        "--no-pr5-pipeline",
        action="store_true",
        help="Disable PR-5 pipeline writes (Steps 5b–5e). Test-only; "
        "production scans never set this.",
    )
    parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    parser.add_argument("--use-sample", action="store_true", help="Use sample data instead of live API")
    parser.add_argument("--lock-timeout", type=float, default=10.0, help="Lock timeout seconds")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    setup_logging(args.verbose)

    lock = FileLock(lock_path("scan"), timeout=args.lock_timeout)
    try:
        with lock:
            settings = get_settings()
            db_path = Path(args.db) if args.db else settings.db_path
            db = Database(db_path=db_path)
            db.connect()
            try:
                result = asyncio.run(run_scan(
                    db=db, settings=settings,
                    market_limit=args.market_limit,
                    use_sample=args.use_sample,
                    max_paper_candidates=args.max_paper_candidates,
                    max_trades_per_wallet=args.max_trades_per_wallet,
                    max_wallet_scores=args.max_wallet_scores,
                    enable_pr5_pipeline=not args.no_pr5_pipeline,
                ))
            finally:
                db.close()
    except LockError as e:
        logger.error("Lock held: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(result.summary())

    if result.errors:
        for err in result.errors[:5]:
            print(f"  ERROR: {err}", file=sys.stderr)
    if result.missing_data:
        for msg in result.missing_data[:5]:
            print(f"  WARN: {msg}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
