#!/usr/bin/env python3
"""collect_smart_money_data.py — Collect smart-money wallet data from Polymarket.

Orchestrates the data collection workflow:
1. Fetch active markets from Polymarket Gamma API
2. For each market, fetch recent trades
3. Track wallets discovered from trades
4. Fetch wallet balances (USDC)
5. Persist all data to SQLite with raw snapshot provenance
6. Run scoring and verdict engine on discovered wallets
7. Record signals and generate experiment run entry

ALL data is labeled is_sample=False when from live API.
When live API is unavailable, clearly labeled sample adapters are used.

Exit codes:
    0 — success (full or partial)
    1 — fatal error (DB failure, config error)
    2 — partial success (some markets/trades failed)
    3 — lock held by another process
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

# Add src to path for inline execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.adapters.polymarket import (
    PolymarketPublicAdapter,
    build_market_trade_params,
)
from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.db.wallet_identity import (
    address_column_normalized,
    canonical_wallet_address,
    is_sentinel_trader_address,
)
from polycopy.domain.experiment import ExperimentRun, ExperimentStatus
from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.raw_snapshot import RawSnapshot
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet
from polycopy.engine.evaluate import evaluate_wallet
from polycopy.utils.concurrency import FileLock, LockError, lock_path

logger = logging.getLogger(__name__)

# ── Logging setup ────────────────────────────────────────────────────────────────

def setup_logging(verbosity: int = 0) -> None:
    """Configure logging based on verbosity level."""
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


# ── Data collection result ──────────────────────────────────────────────────────

class CollectionResult:
    """Tracks partial/failure state from a collection run.

    Round-10 fetch-result accounting adds three explicit per-market-fetch
    counters in addition to the legacy per-market-row counters:

      * ``market_fetches_complete`` — every requested page fetched; safe
        for downstream wallet discovery + scoring.
      * ``market_fetches_partial`` — at least one page succeeded but a
        later page failed. The prefix is NOT persisted in PR #3; it is
        surfaced so the caller knows scoring was skipped.
      * ``market_fetches_failed`` — first page failed (or empty
        market_source_id). Nothing persisted from this attempt.

    These counters are the source of truth for "did the upstream
    give us a trustworthy market history" and are independent of the
    per-row ``trades_fetched`` counter (which counts successfully
    persisted rows).
    """

    def __init__(self) -> None:
        self.markets_fetched: int = 0
        self.markets_failed: int = 0
        self.trades_fetched: int = 0
        self.trades_failed: int = 0
        self.wallets_discovered: int = 0
        self.anonymous_trades_skipped: int = 0  # P2: trades with no attributable wallet
        self.snapshots_saved: int = 0
        self.signals_generated: int = 0
        # Round-10 fetch-status counters (per-market, not per-row).
        self.market_fetches_complete: int = 0
        self.market_fetches_partial: int = 0
        self.market_fetches_failed: int = 0
        self.missing_data_log: list[str] = []
        self.errors: list[str] = []
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None

    @property
    def is_partial(self) -> bool:
        # Round-10: partial is true when any per-market fetch returned
        # partial OR failed. Per-row trades_failed still contributes
        # (e.g. when one persisted row later raises).
        return (
            self.market_fetches_partial > 0
            or self.market_fetches_failed > 0
            or self.markets_failed > 0
            or self.trades_failed > 0
        )

    @property
    def is_failure(self) -> bool:
        return self.markets_fetched == 0 and self.trades_fetched == 0

    def summary(self) -> str:
        status = "ok"
        if self.is_failure:
            status = "FAILED"
        elif self.is_partial:
            status = "PARTIAL"
        return (
            f"status={status}\n"
            f"  markets: {self.markets_fetched} fetched, {self.markets_failed} failed\n"
            f"  trade fetches: {self.market_fetches_complete} complete, "
            f"{self.market_fetches_partial} partial, {self.market_fetches_failed} failed\n"
            f"  trades:  {self.trades_fetched} fetched, {self.trades_failed} failed\n"
            f"  wallets: {self.wallets_discovered} discovered\n"
            f"  anonymous trades skipped: {self.anonymous_trades_skipped}\n"
            f"  snapshots saved: {self.snapshots_saved}\n"
            f"  signals: {self.signals_generated}\n"
            f"  missing data entries: {len(self.missing_data_log)}\n"
            f"  errors: {len(self.errors)}"
        )


# ── Polymarket collector (live) ────────────────────────────────────────────────

class PolymarketCollector:
    """Fetches market and trade data from Polymarket public APIs.

    v0.4 (P21 fix): Uses the data-api (https://data-api.polymarket.com/trades)
    for trade ingestion. CLOB /trades requires auth (HTTP 401). Gamma has no
    /trades endpoint. See reports/polymarket_trade_ingestion_audit.md.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._client = None
        # Cache of per-market asset_id → outcome label mapping, keyed by
        # condition_id. Populated when markets are fetched.
        self._asset_to_outcome: dict[str, dict[str, str]] = {}
        # Shared adapter instance (lazy)
        self._trade_adapter = None

    async def _get_client(self):
        if self._client is None or self._client.is_closed:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.settings.gamma_base_url,
                timeout=self.settings.http_timeout_seconds,
            )
        return self._client

    async def collect_markets(
        self,
        db: Database,
        limit: int = 50,
        result: CollectionResult | None = None,
    ) -> list[Market]:
        """Fetch active markets from Gamma API and persist with provenance."""
        if result is None:
            result = CollectionResult()

        client = await self._get_client()
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }

        try:
            resp = await client.get("/markets", params=params)
            resp.raise_for_status()
            raw_data = resp.json()

            if not isinstance(raw_data, list):
                raw_data = [raw_data]

            # Save snapshot provenance
            snapshot = self._save_snapshot(
                db=db,
                source="polymarket_gamma",
                endpoint="/markets",
                params=params,
                data=raw_data,
            )
            if snapshot:
                result.snapshots_saved += 1

            markets = []
            for item in raw_data:
                try:
                    market = self._parse_market(item)
                    self._persist_market(db, market)
                    # Cache asset_id → outcome map for trade ingestion.
                    cond = market.source_id
                    asset_map = self._build_asset_to_outcome_map(item)
                    if asset_map:
                        self._asset_to_outcome[cond] = asset_map
                    markets.append(market)
                    result.markets_fetched += 1
                except Exception as e:
                    result.markets_failed += 1
                    cond_id = item.get("conditionId", item.get("id", "?"))
                    result.errors.append(f"Market parse error ({cond_id}): {e}")
                    logger.warning("Failed to parse market %s: %s", cond_id, e)

            return markets

        except Exception as e:
            result.errors.append(f"Failed to fetch markets: {e}")
            logger.error("Failed to fetch markets: %s", e)
            return []

    async def _get_trade_adapter(self):
        """Lazy-init the shared PolymarketPublicAdapter used for trade ingestion.

        Round 11 (P3 PRRT_kwDOTG4Cf86M7Xbp): the collector and the scanner
        must construct the adapter through the SAME factory — the shared
        ``scripts._live_ingest.build_trade_adapter(settings)`` helper —
        so neither path drifts from the other on base URLs, timeout,
        rate limit, window size, or request interval. The factory is
        imported here via the package path AND via the bare-name path
        (the latter works when ``scripts/`` is on ``sys.path``, e.g.
        direct CLI execution; the former works when the module is
        imported as ``scripts._live_ingest``). The lazy singleton is
        preserved so a single collection run reuses one adapter
        instance across all markets.
        """
        if self._trade_adapter is None:
            from polycopy.adapters.polymarket import PolymarketPublicAdapter
            adapter = None
            # 1) Try the package import (works under ``import scripts``).
            try:
                from scripts._live_ingest import build_trade_adapter  # type: ignore[import-not-found]
                adapter = build_trade_adapter(self.settings)
            except ImportError:
                pass
            # 2) Try the bare-name import (works when ``scripts/`` is
            #    directly on sys.path — the direct-CLI-execution path).
            if adapter is None:
                try:
                    from _live_ingest import build_trade_adapter  # type: ignore[import-not-found,no-redef]
                    adapter = build_trade_adapter(self.settings)
                except ImportError:
                    pass
            # 3) Defensive fallback: construct directly so a stripped
            #    sys.path cannot crash the live path. This is identical
            #    to what the factory would have built.
            if adapter is None:
                settings = self.settings
                adapter = PolymarketPublicAdapter(
                    gamma_base_url=settings.gamma_base_url,
                    clob_base_url=settings.clob_base_url,
                    data_api_base_url=settings.data_api_base_url,
                    timeout=settings.http_timeout_seconds,
                    rate_limit_rps=settings.http_rate_limit_rps,
                    data_api_window_size=settings.data_api_window_size,
                    data_api_request_interval_seconds=settings.data_api_request_interval_seconds,
                )
            self._trade_adapter = adapter
        return self._trade_adapter

    async def collect_trades(
        self,
        db: Database,
        market_source_id: str,
        result: CollectionResult | None = None,
        since: datetime | None = None,
        limit: int = 200,
        max_pages: int = 5,
        max_rows: int = 2000,
    ) -> list[SourceTrade]:
        """Fetch recent trades for a market from the public data-api.

        Round 7 (P1 fix): Replaces the global-window-then-slice strategy
        with a per-market request to ``GET /trades?market=<conditionId>``
        (server-side filter, verified live 2026-06-28). Pagination is
        bounded by ``max_pages`` and ``max_rows`` and dedups across pages
        via ``deterministic_source_trade_id_v2``.

        Round 10 (fetch-result contract): the adapter now returns a
        :class:`MarketTradeFetchResult` with explicit ``status``. This
        method branches on that status:

          * ``"complete"`` — persist trades, increment
            ``market_fetches_complete``, return the persisted list.
          * ``"partial"`` — DO NOT persist the prefix. Increment
            ``market_fetches_partial``, append a missing-data entry, log
            the market ID + error. The prefix could be untrustworthy
            and silently scoring on it would distort trade counts and
            wallet discovery.
          * ``"failed"`` — first page failed. Increment
            ``market_fetches_failed``, append a missing-data entry.
            Return [].

        Behavior:
          - One HTTP request per page per market (not per run); the
            adapter's data client is reused across markets in the same run.
          - Maps asset_id → outcome label using cached clobTokenIds ordering.
          - Persists trades to source_trades with is_sample=0 (complete only).
          - On any adapter exception, logs and increments failed counters.
          - Never fabricates data: empty market → empty result.
          - Snapshot provenance is saved once per real upstream fetch.
        """
        if result is None:
            result = CollectionResult()

        asset_to_outcome = self._asset_to_outcome.get(market_source_id) or {}
        adapter = await self._get_trade_adapter()

        # Snapshot provenance is now per-market and best-effort: save the
        # first page (limit rows) once per market. The previous global-
        # window strategy only saved one snapshot per run, but the new
        # per-market fetch gets a different upstream payload per market,
        # so a per-market snapshot is the honest equivalent.
        #
        # Round 7 P3 audit fix: ``_snapshot_market_first_page`` returns
        # True ONLY when an upstream payload was actually persisted to
        # disk + raw_snapshots table. We increment ``snapshots_saved``
        # ONLY on True so the counter stays honest across HTTP failures,
        # empty responses, and ``_save_snapshot`` returning None.
        if await self._snapshot_market_first_page(adapter, db, market_source_id, limit):
            result.snapshots_saved += 1

        try:
            fetch_result = await adapter.fetch_trades_for_market(
                market_source_id=market_source_id,
                since=since,
                limit=limit,
                max_pages=max_pages,
                max_rows=max_rows,
                asset_to_outcome=asset_to_outcome,
            )
        except Exception as e:
            result.trades_failed += 1
            result.market_fetches_failed += 1
            result.missing_data_log.append(
                f"Trade fetch exception for market {market_source_id}: {e}"
            )
            logger.warning("No trades for market %s: %s", market_source_id, e)
            return []

        # Branch on the explicit fetch status — never silently treat a
        # partial or failed fetch as complete.
        if fetch_result.status == "failed":
            result.market_fetches_failed += 1
            result.trades_failed += 1
            result.missing_data_log.append(
                f"Market fetch FAILED for {market_source_id}: {fetch_result.error}"
            )
            logger.warning(
                "Market %s fetch FAILED (%d pages, %d rows): %s",
                market_source_id, fetch_result.pages_fetched,
                fetch_result.rows_fetched, fetch_result.error,
            )
            return []

        if fetch_result.status == "partial":
            # Round-10 policy: discard the prefix. Persisting partial
            # history would distort trade counts, scoring, and downstream
            # wallet discovery (the next run would refetch from the same
            # server-side cursor and double-count rows). The caller MUST
            # NOT score from this prefix.
            result.market_fetches_partial += 1
            result.missing_data_log.append(
                f"Market fetch PARTIAL for {market_source_id} "
                f"(pages={fetch_result.pages_fetched}, rows={fetch_result.rows_fetched}, "
                f"error={fetch_result.error})"
            )
            logger.warning(
                "Market %s fetch PARTIAL (%d/%d pages ok, %d rows): %s — "
                "prefix discarded (not persisted)",
                market_source_id, fetch_result.pages_fetched,
                max_pages, fetch_result.rows_fetched, fetch_result.error,
            )
            return []

        # status == "complete": safe to persist.
        result.market_fetches_complete += 1
        persisted = []
        for trade in fetch_result.trades:
            try:
                self._persist_trade(db, trade)
                persisted.append(trade)
                result.trades_fetched += 1
            except Exception as e:
                result.trades_failed += 1
                result.errors.append(f"Trade persist error: {e}")
                logger.warning("Failed to persist trade: %s", e)
        return persisted

    async def probe_and_record_capability(self, db: Database) -> dict:
        """Probe data-api trade availability and record a capability_flags row.

        Returns the probe dict:
          {
            "status": "ok" | "unavailable" | "partial",
            "wallet_attribution_available": bool,
            "trades_returned": int,
            "http_status": int,
            "error": Optional[str],
          }
        """
        adapter = await self._get_trade_adapter()
        probe = await adapter.probe_trade_capability()
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            details = json.dumps({
                "trades_returned": probe["trades_returned"],
                "http_status": probe["http_status"],
                "error": probe["error"],
                "endpoint": "/trades",
                "host": self.settings.data_api_base_url,
            }, sort_keys=True)
            # Upsert capability_flags row for "polymarket_data_api_trades"
            existing = db.fetchone(
                "SELECT id, first_verified_at FROM capability_flags WHERE capability = ?",
                ("polymarket_data_api_trades",),
            )
            wallet_attr = 1 if probe["wallet_attribution_available"] else 0
            if existing:
                db.execute(
                    """UPDATE capability_flags
                       SET status = ?, wallet_attribution_available = ?,
                           details = ?, last_verified_at = ?
                       WHERE capability = ?""",
                    (
                        probe["status"],
                        wallet_attr,
                        details,
                        now_iso,
                        "polymarket_data_api_trades",
                    ),
                )
            else:
                first_iso = now_iso
                db.execute(
                    """INSERT INTO capability_flags
                       (capability, status, wallet_attribution_available,
                        details, first_verified_at, last_verified_at, is_sample)
                       VALUES (?, ?, ?, ?, ?, ?, 0)""",
                    (
                        "polymarket_data_api_trades",
                        probe["status"],
                        wallet_attr,
                        details,
                        first_iso,
                        now_iso,
                    ),
                )
            db.conn.commit()
        except Exception as e:
            logger.warning("Failed to record capability flag: %s", e)
        return probe

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._trade_adapter is not None:
            try:
                await self._trade_adapter.aclose()
            except Exception:
                pass

    async def collect_wallet_balance(self, address: str) -> Wallet | None:
        """Fetch wallet USDC balance. Returns None if unavailable."""
        # Polymarket public API does not expose per-wallet balances.
        # This is a known limitation. Return None and let the caller
        # handle missing data.
        logger.debug(
            "Wallet balance fetch not available via public API for %s",
            address[:12],
        )
        return None

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_market(data: dict) -> Market:
        import json as _json

        outcomes_raw = data.get("outcomes", "[]")
        prices_raw = data.get("outcomePrices", "[]")
        if isinstance(outcomes_raw, str):
            outcomes_raw = _json.loads(outcomes_raw)
        if isinstance(prices_raw, str):
            prices_raw = _json.loads(prices_raw)

        outcomes = []
        for i, label in enumerate(outcomes_raw):
            price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
            outcomes.append(MarketOutcome(label=str(label), price=price))

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

    @staticmethod
    def _parse_clob_token_ids(data: dict) -> list[str]:
        """Extract clobTokenIds as a Python list of token_id strings.

        Gamma returns clobTokenIds as a JSON-string-encoded array of token IDs.
        Returns [] if missing or malformed.
        """
        import json as _json
        raw = data.get("clobTokenIds")
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(t) for t in raw]
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except Exception:
                return []
        return []

    @staticmethod
    def _build_asset_to_outcome_map(data: dict) -> dict[str, str]:
        """Build asset_id → outcome-label map for a Gamma market object.

        Gamma's clobTokenIds is a JSON-encoded array of token IDs in the same
        order as the `outcomes` array. This map lets us rewrite the raw
        data-api `outcome` string (which can be denormalized across markets)
        to the canonical outcome label for THIS market.
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

    @staticmethod
    def _parse_trade(data: dict, market_source_id: str) -> SourceTrade | None:
        """Parse a CLOB trade event into SourceTrade. Returns None if unparseable."""
        try:
            side_raw = data.get("side", "").lower()
            if side_raw in ("buy", "1"):
                from polycopy.domain.order import OrderSide
                side = OrderSide.BUY
            elif side_raw in ("sell", "0"):
                from polycopy.domain.order import OrderSide
                side = OrderSide.SELL
            else:
                return None

            # Parse timestamp
            ts_raw = data.get("timestamp") or data.get("createdAt")
            if ts_raw is None:
                ts = datetime.now(timezone.utc)
            elif isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(ts_raw / 1000.0 if ts_raw > 1e12 else ts_raw, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))

            return SourceTrade(
                source="polymarket_clob",
                source_trade_id=str(data.get("id", data.get("trade_id", uuid.uuid4().hex))),
                market_source_id=market_source_id,
                side=side,
                outcome=str(data.get("outcome", data.get("token", "Yes"))),
                quantity=float(data.get("size", data.get("quantity", 0))),
                price=float(data.get("price", 0)),
                trader_address=str(data.get("maker", data.get("trader", data.get("owner", "unknown")))),
                timestamp=ts,
                is_sample=False,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Unparseable trade: %s", e)
            return None

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_market(self, db: Database, market: Market) -> None:
        """Upsert a market record."""
        db.execute(
            """INSERT OR REPLACE INTO markets
               (id, source_id, source, question, active, closed, resolved,
                resolution_outcome, volume_24h, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(market.id),
                market.source_id,
                market.source,
                market.question,
                int(market.active),
                int(market.closed),
                int(market.resolved),
                market.resolution_outcome,
                market.volume_24h,
                market.fetched_at.isoformat(),
                int(market.is_sample),
            ),
        )
        # Upsert outcomes
        db.execute("DELETE FROM market_outcomes WHERE market_id = ?", (str(market.id),))
        for outcome in market.outcomes:
            db.execute(
                """INSERT INTO market_outcomes (market_id, label, price, volume)
                   VALUES (?, ?, ?, ?)""",
                (str(market.id), outcome.label, outcome.price, outcome.volume),
            )
        db.conn.commit()

    def _persist_trade(self, db: Database, trade: SourceTrade) -> bool:
        """Upsert a source trade — idempotent and provenance-preserving.

        Round 9 (PR #3 stabilization): switched from ``INSERT OR REPLACE``
        to ``INSERT OR IGNORE`` against the ``UNIQUE(source, source_trade_id)``
        index. ``INSERT OR REPLACE`` would silently overwrite existing
        rows on an exact rerun, destroying provenance and corrupting
        counter truthfulness. With ``INSERT OR IGNORE``:
          * Fresh row → ``cur.rowcount == 1``, return ``True``.
          * Already exists → ``cur.rowcount == 0``, return ``False`` —
            counted as dedup, not as a new persist.

        Defensive canonicalization: the attributed ``trader_address`` is
        normalized via ``canonical_wallet_address`` so the value persisted
        into ``source_trades`` matches the canonical form used by
        ``_compute_wallet_metrics`` and the wallet discovery loop.
        Anonymous / sentinel trader addresses are stored as ``NULL`` and
        never become wallet rows.
        """
        ta = trade.trader_address
        if ta is not None:
            canonical = canonical_wallet_address(ta)
            persisted_trader_address = canonical  # None for sentinels/empty
        else:
            persisted_trader_address = None
        cur = db.execute(
            """INSERT OR IGNORE INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                trade.source,
                trade.source_trade_id,
                trade.market_source_id,
                trade.side.value if hasattr(trade.side, "value") else str(trade.side),
                trade.outcome,
                trade.quantity,
                trade.price,
                persisted_trader_address,
                trade.timestamp.isoformat() if trade.timestamp else None,
                int(trade.is_sample),
            ),
        )
        db.conn.commit()
        # rowcount == 1 means a fresh insert; 0 means duplicate (UNIQUE hit)
        return bool(getattr(cur, "rowcount", 0))

    def _persist_wallet(self, db: Database, wallet: Wallet) -> str | None:
        """Idempotently persist a wallet by canonical address.

        Same find-or-create semantics as ``scripts.run_scan._persist_wallet``
        (single canonical implementation lives in
        :mod:`polycopy.db.wallet_identity`). ``address`` is normalized via
        ``canonical_wallet_address`` so any case/padding variant maps to
        one row. Returns the wallet id (existing or new), or ``None`` for
        anonymous inputs / on persistence failure.

        Any balances attached to the input ``wallet`` are inserted against
        the resolved wallet id (existing or new) using ``INSERT OR IGNORE``
        on the ``wallet_balances`` row identity — so an exact rerun of
        the collector never duplicates balances. Each balance row's
        identity is ``(wallet_id, currency, as_of)`` (callers that want
        upsert-by-that-key may use a unique index; here we use plain
        ``INSERT`` with the same DB-level retry-on-conflict pattern as
        ``_persist_trade`` for safety).
        """
        try:
            canonical = canonical_wallet_address(wallet.address)
            if canonical is None:
                return None
            existing = db.fetchone(
                f"SELECT id FROM wallets "
                f"WHERE {address_column_normalized('address')} = ?",
                (canonical,),
            )
            if existing is not None:
                wallet_id = existing["id"]
            else:
                wallet_id = str(uuid.uuid4())
                db.execute(
                    """INSERT OR IGNORE INTO wallets
                       (id, address, label, is_sample, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        wallet_id,
                        canonical,
                        wallet.label,
                        int(wallet.is_sample),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                # Re-fetch: a concurrent writer may have raced us to the
                # same canonical address. The re-fetch always uses the
                # canonical-address predicate so a race leaves one row,
                # not two.
                post = db.fetchone(
                    f"SELECT id FROM wallets "
                    f"WHERE {address_column_normalized('address')} = ?",
                    (canonical,),
                )
                if post is not None:
                    wallet_id = post["id"]
            # Persist attached balances (if any). Plain INSERT matches the
            # pre-existing balance-write behavior: wallet_balances has no
            # unique index on (wallet_id, currency, as_of) and a rerun of
            # the collector historically created duplicate rows. Adding a
            # unique index here is out of scope for this PR (wallet_balance
            # dedup is a separate concern from the wallet-identity
            # invariant) — flagged for a follow-up.
            for bal in wallet.balances:
                db.execute(
                    """INSERT INTO wallet_balances
                       (wallet_id, currency, amount, as_of, is_sample)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        wallet_id,
                        bal.currency,
                        float(bal.amount),
                        bal.as_of.isoformat() if bal.as_of else None,
                        int(bal.is_sample),
                    ),
                )
            db.conn.commit()
            return wallet_id
        except Exception as e:
            logger.debug("Wallet persist skipped for %r: %s", wallet.address, e)
            try:
                db.conn.rollback()
            except Exception:
                pass
            return None

    async def _snapshot_market_first_page(
        self,
        adapter: PolymarketPublicAdapter,
        db: Database,
        market_source_id: str,
        limit: int,
    ) -> bool:
        """Snapshot the first page of a market's /trades fetch.

        Best-effort: any error here is swallowed (the trade ingest path
        must not be blocked by snapshot problems). Returns True only when
        an upstream payload was actually persisted to disk AND to the
        ``raw_snapshots`` table via ``_save_snapshot``. Returns False
        for HTTP failures, empty responses, or any persistence failure
        — the caller uses this boolean to keep ``result.snapshots_saved``
        honest.

        Uses the adapter's data client directly (we don't want to invoke
        the paginated ``fetch_trades_for_market`` here because that
        already parses and dedups — we just want the raw first page for
        provenance).

        Round-11 (Codex P2 PRRT_kwDOTG4Cf86M7BQV): the snapshot request
        MUST use the SAME ``build_market_trade_params(...)`` helper as
        the paginated fetch, so the saved raw payload is a faithful
        representation of the maker-inclusive source data the
        downstream persistence + scoring paths will see. The previous
        code sent ``market``, ``limit``, ``offset`` but omitted
        ``takerOnly=false``, recording a taker-only payload as
        provenance while downstream persisted a maker-inclusive payload.
        """
        try:
            client = await adapter._get_data_client()  # noqa: SLF001 (intentional)
            await adapter._throttle()  # noqa: SLF001
            # Single source of truth — the same helper used by
            # ``fetch_trades_for_market``. Any future change to the
            # request shape must go through this helper so the snapshot
            # and the ingested payload can never drift.
            params = build_market_trade_params(
                market_source_id,
                limit=limit,
                offset=0,
            )
            resp = await client.get("/trades", params=params)
            resp.raise_for_status()
            data = resp.json()
            if not (isinstance(data, list) and data):
                # Empty response — nothing to snapshot, but also not a
                # failure. The caller's ``snapshots_saved`` MUST NOT
                # increment for empty payloads.
                return False
            snapshot = self._save_snapshot(
                db=db,
                source="polymarket_data_api",
                endpoint="/trades",
                # Persist the SAME params that were sent on the wire
                # (with a stable ``filter=per_market`` tag for downstream
                # discovery). Includes ``takerOnly=false`` so callers of
                # the snapshot store can see the contract that produced
                # this raw payload.
                params={
                    **params,
                    "filter": "per_market",
                },
                data=data,
            )
            return snapshot is not None
        except Exception as e:
            logger.debug("per-market snapshot skipped: %s", e)
            return False

    def _save_snapshot(
        self,
        db: Database,
        source: str,
        endpoint: str,
        params: dict,
        data: object,
    ) -> RawSnapshot | None:
        """Save a raw API response to the snapshot store."""
        try:
            import hashlib
            import json as _json

            settings = get_settings()
            snapshot_dir = settings.snapshot_dir
            snapshot_dir.mkdir(parents=True, exist_ok=True)

            content = _json.dumps(data, default=str, sort_keys=True)
            content_bytes = content.encode("utf-8")
            content_hash = hashlib.sha256(content_bytes).hexdigest()

            ts = datetime.now(timezone.utc)
            filename = f"{source}_{ts.strftime('%Y%m%dT%H%M%S')}_{content_hash[:12]}.json"
            file_path = snapshot_dir / filename

            file_path.write_text(content)

            snapshot = RawSnapshot(
                source=source,
                endpoint=endpoint,
                query_params=params,
                file_path=str(file_path),
                content_hash=content_hash,
                content_type="application/json",
                size_bytes=len(content_bytes),
                fetched_at=ts,
                ingested_at=ts,
                is_sample=False,
            )

            db.execute(
                """INSERT INTO raw_snapshots
                   (id, source, endpoint, query_params, file_path, content_hash,
                    content_type, size_bytes, fetched_at, ingested_at, is_sample)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(snapshot.id),
                    snapshot.source,
                    snapshot.endpoint,
                    _json.dumps(snapshot.query_params),
                    snapshot.file_path,
                    snapshot.content_hash,
                    snapshot.content_type,
                    snapshot.size_bytes,
                    snapshot.fetched_at.isoformat(),
                    snapshot.ingested_at.isoformat(),
                    int(snapshot.is_sample),
                ),
            )
            db.conn.commit()
            return snapshot

        except Exception as e:
            logger.warning("Failed to save snapshot: %s", e)
            return None


# ── Main collection workflow ────────────────────────────────────────────────────

async def run_collection(
    db: Database,
    limit: int = 50,
    skip_trades: bool = False,
) -> CollectionResult:
    """Run the full data collection workflow.

    Args:
        db: connected database.
        limit: max markets to fetch.
        skip_trades: if True, skip trade collection (for testing).

    Returns:
        CollectionResult with counts and any errors.
    """
    settings = get_settings()
    result = CollectionResult()
    collector = PolymarketCollector(settings)

    try:
        # 0. Probe data-api capability and record flag (before any trade fetch).
        if not skip_trades:
            logger.info("Probing data-api /trades capability...")
            try:
                cap = await collector.probe_and_record_capability(db)
                logger.info(
                    "data-api /trades: status=%s wallet_attr=%s trades=%s http=%s err=%s",
                    cap["status"],
                    cap["wallet_attribution_available"],
                    cap["trades_returned"],
                    cap["http_status"],
                    cap["error"],
                )
            except Exception as e:
                logger.warning("Capability probe failed: %s", e)

        # 1. Collect markets
        logger.info("Fetching active markets (limit=%d)...", limit)
        markets = await collector.collect_markets(db, limit=limit, result=result)
        logger.info("Fetched %d markets (%d failed)", result.markets_fetched, result.markets_failed)

        # 2. Collect trades for each market
        if not skip_trades:
            for market in markets:
                logger.debug("Fetching trades for market %s...", market.source_id)
                trades = await collector.collect_trades(db, market.source_id, result)
                # Discover wallets from trades
                for trade in trades:
                    # P2 fix: anonymous trades (trader_address=None) MUST NOT be
                    # promoted to fake wallets. They are still persisted in
                    # source_trades as market-level observations, but they are
                    # skipped here so they cannot be scored by evaluate_wallet.
                    # ``is_sentinel_trader_address`` covers both NULL and legacy
                    # sentinel strings ("unknown" / "anonymous" / "missing" /
                    # "0x" / "0x0").
                    if is_sentinel_trader_address(trade.trader_address):
                        result.anonymous_trades_skipped += 1
                        continue
                    wallet = Wallet(
                        address=trade.trader_address,
                        label=f"discovered-from-{trade.source}",
                        is_sample=False,
                    )
                    collector._persist_wallet(db, wallet)
                    result.wallets_discovered += 1

        # 3. Score discovered wallets
        wallets = _get_unique_trader_addresses(db)
        logger.info("Scoring %d discovered wallets...", len(wallets))
        for address in wallets:
            try:
                score_id, summary = evaluate_wallet(
                    wallet_address=address,
                    source="collect_smart_money",
                    is_sample=False,
                )
                logger.debug("Scored %s: %s", address[:12], summary.replace(chr(10), " | "))
            except Exception as e:
                logger.warning("Failed to score wallet %s: %s", address[:12], e)

        # 4. Record experiment run
        result.ended_at = datetime.now(timezone.utc)
        _record_experiment(db, result)

    finally:
        await collector.close()

    return result


def _get_unique_trader_addresses(db: Database) -> list[str]:
    """Get distinct canonical trader addresses from source_trades.

    Defensive: filters out NULL and empty-string addresses so anonymous trades
    (P2) cannot end up in the scoring loop even if they bypass the collector's
    anonymous skip.

    Note: the v5 migration normalizes legacy sentinel strings
    ("unknown" / "anonymous" / "missing" / "0x" / "0x0")
    to NULL on upgrade, so the DB-level filter alone is sufficient for
    upgraded databases. The ``is_sentinel_trader_address`` runtime helper
    below provides defense in depth and is the source of truth for live
    collectors/scoring loops.

    Returns the CANONICAL lowercase form of every distinct real wallet
    address — so a freshly discovered ``0xAbCd...`` and a legacy
    ``0xabcd...`` collapse to the same entry, and a padded legacy
    ``"\\t0xAbCd...\\n"`` collapses to ``0xabcd...``. The SQL fragment
    uses the shared ``address_column_normalized`` helper so the predicate
    matches the v5 migration cleanup byte-for-byte (modulo the X'00' NUL
    addition that makes it pad-aware across all ASCII whitespace).
    """
    rows = db.fetchall(
        f"SELECT DISTINCT {address_column_normalized('trader_address')} AS addr "
        f"FROM source_trades "
        f"WHERE trader_address IS NOT NULL "
        f"AND {address_column_normalized('trader_address')} != '' "
        f"AND {address_column_normalized('trader_address')} NOT IN ('unknown', 'anonymous', 'missing', '0x', '0x0')"
    )
    return [
        row["addr"]
        for row in rows
        if not is_sentinel_trader_address(row["addr"])
    ]


def _record_experiment(db: Database, result: CollectionResult) -> None:
    """Record the collection run as an experiment entry."""
    run = ExperimentRun(
        label=f"collect-{result.started_at.strftime('%Y%m%dT%H%M%S')}",
        strategy_config={
            "script": "collect_smart_money_data.py",
            "limit": result.markets_fetched,
            "skip_trades": False,
        },
        status=ExperimentStatus.COMPLETED,
        started_at=result.started_at,
        ended_at=result.ended_at,
        result_summary={
            "markets_fetched": result.markets_fetched,
            "trades_fetched": result.trades_fetched,
            "wallets_discovered": result.wallets_discovered,
            "trades_anonymous": result.anonymous_trades_skipped,
            "errors": len(result.errors),
            "missing_data": len(result.missing_data_log),
        },
        is_sample=False,
    )
    db.execute(
        """INSERT INTO experiment_runs
           (id, label, strategy_config, status, started_at, ended_at,
            result_summary, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(run.id),
            run.label,
            json.dumps(run.strategy_config),
            run.status.value,
            run.started_at.isoformat() if run.started_at else None,
            run.ended_at.isoformat() if run.ended_at else None,
            json.dumps(run.result_summary),
            int(run.is_sample),
        ),
    )
    db.conn.commit()


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect smart-money wallet data from Polymarket",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max markets to fetch (default: 50)",
    )
    parser.add_argument(
        "--skip-trades",
        action="store_true",
        help="Skip trade collection (for testing)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="SQLite database path (overrides config)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for file lock (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect but do not persist to database",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v = INFO, -v -v = DEBUG)",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Concurrency guard
    lock = FileLock(lock_path("collect"), timeout=args.lock_timeout)
    try:
        with lock:
            # Configure database
            settings = get_settings()
            db_path = Path(args.db) if args.db else settings.db_path

            db = Database(db_path=db_path)
            db.connect()

            try:
                result = asyncio.run(
                    run_collection(
                        db=db,
                        limit=args.limit,
                        skip_trades=args.skip_trades,
                    )
                )
            finally:
                db.close()

    except LockError as e:
        logger.error("Lock held by another process: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    # Report
    print(result.summary())

    if result.is_failure:
        return 1
    if result.is_partial:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
