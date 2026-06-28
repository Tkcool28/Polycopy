"""Shared helper for live Polymarket trade ingestion.

Both ``scripts/run_scan.py`` (P2 fix) and ``scripts/collect_smart_money_data.py``
need to ingest live trades from the public data-api. They MUST consume the
SAME adapter code path (single fetch per scan, cached window, identical
normalization) so that:

  * ``source_trade_id`` is the same whether a row came in via run_scan or the
    collector (P1 invariant — keep in sync with
    ``polycopy.adapters.polymarket.deterministic_source_trade_id_v2``).
  * ``run_scan`` and the collector don't make duplicate HTTP calls against
    the data-api.
  * The snapshot provenance is written exactly once per real upstream fetch
    (P3 invariant — keep in sync with ``PolymarketPublicAdapter._fetch_global_window``).

This module is a thin wrapper:

  * :func:`build_trade_adapter` returns a single shared
    :class:`PolymarketPublicAdapter` constructed from the active ``Settings``.
  * :func:`fetch_recent_trades_for_market` calls the adapter's
    ``_fetch_global_window`` (which handles the cache) and then
    ``get_recent_trades`` to slice per market.

The actual fetching and parsing live in
``polycopy.adapters.polymarket.PolymarketPublicAdapter`` — this helper exists
only so both scripts share a SINGLE construction path.
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
    since: datetime,
    limit: int = 200,
) -> list[SourceTrade]:
    """Fetch recent trades for a single market via the shared adapter.

    Returns an empty list on any adapter error — never raises. The shared
    adapter's ``_fetch_global_window`` is the single source of truth for
    upstream calls, so this is safe to invoke in a tight loop across many
    markets within a single run.
    """
    try:
        return await adapter.get_recent_trades(
            market_source_id=market_source_id,
            since=since,
            limit=limit,
        )
    except Exception:
        # Adapter already logs the underlying error; return [] for the
        # orchestrator to treat as a degraded/empty market slice.
        return []
