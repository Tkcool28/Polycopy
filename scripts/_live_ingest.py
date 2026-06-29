"""Shared helper for live Polymarket trade ingestion.

Both ``scripts/run_scan.py`` (P2 fix) and ``scripts/collect_smart_money_data.py``
need to ingest live trades from the public data-api. They MUST consume the
SAME adapter code path (single shared ``PolymarketPublicAdapter`` instance,
identical normalization, identical ``source_trade_id``) so that:

  * ``source_trade_id`` is the same whether a row came in via run_scan or the
    collector (P1 invariant — keep in sync with
    ``polycopy.adapters.polymarket.deterministic_source_trade_id_v2``).
  * ``run_scan`` and the collector share one per-market data-api fetch path
    instead of maintaining divergent parsing/snapshot behavior.
  * The snapshot provenance is written once per real upstream per-market fetch
    (P3 invariant — keep in sync with ``PolymarketPublicAdapter.fetch_trades_for_market``).

This module is a thin wrapper:

  * :func:`build_trade_adapter` returns a single shared
    :class:`PolymarketPublicAdapter` constructed from the active ``Settings``.
  * :func:`fetch_recent_trades_for_market` calls the adapter's
    ``fetch_trades_for_market`` (round 7: server-side ``?market=<id>``
    filter with bounded pagination) and is the SINGLE entry point used by
    both the per-market collector and the run_scan live path.

The actual fetching and parsing live in
``polycopy.adapters.polymarket.PolymarketPublicAdapter`` — this helper exists
only so both scripts share a SINGLE construction path AND a SINGLE per-market
fetch implementation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# Make repo importable when called from outside the package.
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.config.settings import Settings, get_settings  # noqa: E402
from polycopy.domain.source_trade import SourceTrade  # noqa: E402

# Re-export so callers can ``from _live_ingest import PolymarketPublicAdapter``.
__all__ = [
    "PolymarketPublicAdapter",
    "Settings",
    "build_trade_adapter",
    "fetch_recent_trades_for_market",
    "get_settings",
]


def build_trade_adapter(settings: Optional[Settings] = None) -> PolymarketPublicAdapter:
    """Construct a PolymarketPublicAdapter wired to the active Settings.

    Callers should reuse the SAME adapter instance across a single scan /
    collection run so that the cache and the per-instance global window state
    are shared. This is the single construction point for both scripts.
    """
    s = settings or get_settings()
    return PolymarketPublicAdapter(
        gamma_base_url=s.gamma_base_url,
        clob_base_url=s.clob_base_url,
        data_api_base_url=s.data_api_base_url,
        timeout=s.http_timeout_seconds,
        rate_limit_rps=s.http_rate_limit_rps,
        data_api_window_size=s.data_api_window_size,
        data_api_request_interval_seconds=s.data_api_request_interval_seconds,
    )


async def fetch_recent_trades_for_market(
    adapter: PolymarketPublicAdapter,
    *,
    market_source_id: str,
    since: Optional[datetime] = None,
    limit: int = 200,
    max_pages: int = 5,
    max_rows: int = 2000,
) -> list[SourceTrade]:
    """Fetch recent trades for a single market via the shared adapter.

    Round 7: this delegates to ``adapter.fetch_trades_for_market`` which
    uses ``GET /trades?market=<conditionId>`` (server-side filter,
    verified live 2026-06-28) with bounded pagination and dedup across
    pages. It is the SINGLE per-market entry point used by both
    ``scripts/run_scan.py`` and ``scripts/collect_smart_money_data.py``.

    Returns an empty list on any adapter error — never raises. The shared
    adapter instance is reused so cached resources are shared across
    markets within a single run.
    """
    try:
        return await adapter.fetch_trades_for_market(
            market_source_id=market_source_id,
            since=since,
            limit=limit,
            max_pages=max_pages,
            max_rows=max_rows,
        )
    except Exception:
        # Adapter already logs the underlying error; return [] for the
        # orchestrator to treat as a degraded/empty market slice.
        return []
