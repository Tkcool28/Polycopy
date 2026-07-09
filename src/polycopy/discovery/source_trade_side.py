"""PR24T — Canonical source_trades.side normalization for persistence.

This module is the single, strict normalization boundary for values that will
be PERSISTED to ``source_trades.side``. It is NOT the defensive bridge
canonicalization used by PR24R/PR24S (that lives in
``trade_copyability_bridge_audit.canonicalize_source_side`` and only reports
blockers; it never raises).

Why a separate helper?

* The defensive bridge canonicalization treats malformed/missing side as a
  report-only blocker (returns ``canonical_side=None`` + a reason). That is
  correct for an audit/read-only bridge, but it must NOT be reused as the
  persistence gate, because a persistence gate must FAIL LOUDLY on unknown
  input rather than silently store ``None``/empty.
* PR24R/PR24S found the production ``source_trades.side`` column already has
  mixed casing (``buy=4``, ``BUY=1``). That inconsistency originates upstream
  in the ingestion/writer path (``wallet_discovery.py`` ``process_trade`` /
  ``TrackedTrade`` boundary, fed from ``adapters/polymarket.py``). This helper
  is applied at that write boundary so FUTURE rows are always canonical.
* Existing production rows are NOT backfilled by this PR (see guardrails).
  The bridge reports stay honest and still show raw casing.

Rules (strict — raises on anything that is not a known logical side):

* Accepts only logical BUY/SELL strings, case-insensitive after trimming.
* ``"buy"`` / ``"BUY"`` / ``" Buy "`` -> ``"BUY"``
* ``"sell"`` / ``"SELL"`` / ``" Sell "`` -> ``"SELL"``
* ``None`` / blank / unknown / malformed -> raises ``ValueError``

It does NOT silently convert unknowns. It does NOT default missing side to
``BUY``. This keeps the writer honest: bad data must be fixed at source, not
hidden.
"""

from __future__ import annotations

from typing import Any

_VALID_BUY = frozenset({"buy"})
_VALID_SELL = frozenset({"sell"})


def normalize_source_trade_side_for_persistence(side: Any) -> str:
    """Return the canonical persisted side string for a source_trades write.

    Accepts only logical BUY/SELL (case-insensitive, trimmed). Raises
    ``ValueError`` on ``None``, blank, or any unknown/malformed value so the
    caller cannot silently persist an inconsistent or incorrect side.

    Examples:
        >>> normalize_source_trade_side_for_persistence("buy")
        'BUY'
        >>> normalize_source_trade_side_for_persistence("BUY")
        'BUY'
        >>> normalize_source_trade_side_for_persistence(" Buy ")
        'BUY'
        >>> normalize_source_trade_side_for_persistence("sell")
        'SELL'
        >>> normalize_source_trade_side_for_persistence("SELL")
        'SELL'
        >>> normalize_source_trade_side_for_persistence(" Sell ")
        'SELL'
        >>> normalize_source_trade_side_for_persistence("")        # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: invalid source_trades.side: '' (expected BUY or SELL)
        >>> normalize_source_trade_side_for_persistence(None)      # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: invalid source_trades.side: None (expected BUY or SELL)
        >>> normalize_source_trade_side_for_persistence("BID")      # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: invalid source_trades.side: 'BID' (expected BUY or SELL)
    """
    if side is None:
        raise ValueError("invalid source_trades.side: None (expected BUY or SELL)")
    normalized = str(side).strip().lower()
    if normalized == "":
        raise ValueError("invalid source_trades.side: '' (expected BUY or SELL)")
    if normalized in _VALID_BUY:
        return "BUY"
    if normalized in _VALID_SELL:
        return "SELL"
    raise ValueError(
        f"invalid source_trades.side: {side!r} (expected BUY or SELL)"
    )


__all__ = ["normalize_source_trade_side_for_persistence"]
