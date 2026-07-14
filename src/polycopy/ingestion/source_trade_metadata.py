"""Canonical, versioned metadata evidence for ``source_trades``.

This module deliberately preserves only the bounded PR66 evidence contract.
Unknown upstream fields are excluded rather than becoming accidental schema.
Event slugs remain event identity; this module never assigns specialist or
category-scoring labels.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

METADATA_VERSION = "1"
_CANONICAL_SEPARATORS = (",", ":")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar(value: Any) -> str | None:
    """Return a normalized scalar string, or ``None`` for unusable values."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _first_scalar(*values: Any) -> str | None:
    for value in values:
        normalized = _scalar(value)
        if normalized is not None:
            return normalized
    return None


def _tags(value: Any) -> list[str]:
    """Normalize raw tags to a deterministic, de-duplicated string list."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    normalized = {_scalar(item) for item in value}
    return sorted(tag for tag in normalized if tag is not None)


def normalize_source_trade_metadata(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the exact PR66 metadata contract from an upstream-like mapping.

    The function accepts either raw upstream fields (``eventId``, ``category``,
    ``series``) or an already canonical-shaped mapping. It never copies unknown
    fields, and malformed sections safely become their null/default forms.
    """
    source = _mapping(raw)
    event = _mapping(source.get("event"))
    taxonomy = _mapping(source.get("taxonomy"))
    series = _mapping(source.get("series"))

    return {
        "metadata_version": METADATA_VERSION,
        "event": {
            "id": _first_scalar(event.get("id"), source.get("eventId")),
            "slug": _first_scalar(event.get("slug"), source.get("eventSlug")),
            "title": _first_scalar(event.get("title"), source.get("eventTitle")),
        },
        "taxonomy": {
            "raw_category": _first_scalar(
                taxonomy.get("raw_category"), taxonomy.get("category"), source.get("category")
            ),
            "tags": _tags(taxonomy.get("tags") if "tags" in taxonomy else source.get("tags")),
        },
        "series": {
            "id": _first_scalar(series.get("id"), source.get("seriesId")),
            "slug": _first_scalar(series.get("slug"), source.get("seriesSlug")),
            "title": _first_scalar(series.get("title"), source.get("seriesTitle")),
            "ticker": _first_scalar(series.get("ticker"), source.get("ticker")),
        },
    }


def build_metadata_from_gamma_market(
    trade: Mapping[str, Any] | None,
    gamma_market: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the canonical PR66 metadata contract from a trusted Gamma market.

    Used by PR68 bounded approved-wallet ingestion. The approved-wallet trade
    data-api endpoint does NOT return event/taxonomy/series fields, so the
    trusted taxonomy/event/series evidence is sourced from the Gamma market
    that the trade's ``conditionId`` resolves to (the same trusted source
    PR67's ``resolve_category_label_for_inputs`` already uses).

    Gamma raw-market shape: ``events`` is a LIST of event objects, ``series``
    is likewise a LIST (or a single object). We take the first element of each
    (Gamma markets resolve to exactly one event/series) and feed it to the
    canonical normalizer.

    Trusted taxonomy source — EXACTLY the PR66 contract, no more:
      * ``raw_category`` comes ONLY from Gamma ``category``. This matches the
        trusted source already used by PR67's
        ``resolve_category_label_for_inputs`` (``category`` / ``category_label``).
      * ``groupItemTitle`` is NOT a trusted taxonomy field. There is NO
        repository or upstream-schema evidence that ``groupItemTitle`` is a
        category; it is a display grouping only and is intentionally NOT read.
      * ``tags`` comes ONLY from an explicit Gamma ``tags`` list.
      * ``event`` / ``series`` come ONLY from Gamma ``events`` / ``series``
        objects (id/slug/title). ``event.slug`` is identity, never a category.
      * If Gamma provides nothing, the result is the canonical all-null shape
        (taxonomy stays UNAVAILABLE honestly).

    Strict no-inference rules (mirrors ``normalize_source_trade_metadata``):
      * ``raw_category`` is never derived from title, ``groupItemTitle``,
        event/slug, token, or outcome text.

    Args:
        trade: the upstream-like trade dict (may carry nothing useful for
            taxonomy; kept for forward-compat only).
        gamma_market: the parsed Gamma market object/dict. May be None when
            the market cannot be resolved (taxonomy then stays UNAVAILABLE).

    Returns:
        The exact same canonical dict shape as :func:`normalize_source_trade_metadata`.
    """
    trade_map = _mapping(trade)
    market = _mapping(gamma_market)
    # Prefer explicit trade-level taxonomy when the upstream trade actually
    # carries it (future-proofing); otherwise fall back to Gamma.
    if any(trade_map.get(k) for k in ("event", "taxonomy", "series", "category", "tags")):
        source = trade_map
    else:
        # Adapt Gamma's plural events/series lists to the canonical singular
        # event/series shape before normalizing.
        source = dict(market)
        events = market.get("events")
        if isinstance(events, list) and events:
            source["event"] = events[0]
        series = market.get("series")
        if isinstance(series, list) and series:
            source["series"] = series[0]
    return normalize_source_trade_metadata(source)


def serialize_source_trade_metadata(raw: Mapping[str, Any] | None) -> str:
    """Serialize canonical source-trade evidence deterministically."""
    return json.dumps(
        normalize_source_trade_metadata(raw),
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
    )


def serialize_gamma_market_metadata(
    trade: Mapping[str, Any] | None,
    gamma_market: Mapping[str, Any] | None,
) -> str:
    """Serialize the canonical Gamma-derived metadata deterministically."""
    return json.dumps(
        build_metadata_from_gamma_market(trade, gamma_market),
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
    )


__all__ = [
    "METADATA_VERSION",
    "build_metadata_from_gamma_market",
    "normalize_source_trade_metadata",
    "serialize_gamma_market_metadata",
    "serialize_source_trade_metadata",
]
