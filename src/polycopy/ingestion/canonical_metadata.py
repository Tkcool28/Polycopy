"""Single canonical metadata builder for the research evidence plane.

This module is the ONE authoritative producer of the nested canonical
``metadata_json`` shape that the specialist taxonomy scorer reads
(``source_trades.metadata_json["taxonomy"]["raw_category"]``). Every
evidence path MUST route Gamma-derived metadata through this builder so the
output is byte-equivalent across:

  * new research collection (``specialist_evidence_collector``)
  * historical taxonomy backfill (``backfill_specialist_trade_taxonomy``)
  * repaired per-trade enrichment (``enrich_approved_source_trade``)
  * existing approved-wallet collector (``source_trade_metadata`` delegates here)

Design invariants
-----------------
* The scorer-facing taxonomy lives ONLY in ``metadata["taxonomy"]["raw_category"]``.
  ``source_trade_enrichments.normalized_category`` is audit-only and is never a
  scoring authority.
* We NEVER infer taxonomy from a market title or question text, and we NEVER
  read taxonomy from the raw trade row. The SOLE authority for canonical
  taxonomy/event/series is the trusted Gamma market.
* Immutable identity/economic columns (side, outcome, price, quantity,
  timestamp, token_id, market_source_id, source_trade_id) are never written by
  this module.
* Gamma markets are accepted as ``collections.abc.Mapping`` (the plan's
  ``FakeMarket`` protocol: ``category``, ``tags``, ``events``, ``series``).
* ``metadata_version`` is stamped on EVERY merge output (filled, unchanged, and
  preserved-existing shapes) so downstream consumers can detect drift.
* Conflicts are fail-closed: any non-empty, differing value across the
  taxonomy / event / series namespaces blocks the merge (status ``conflict``);
  the caller must NOT overwrite.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Optional

from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_USABLE,
    OfficialPolymarketTaxonomyResolverV1,
    OfficialTaxonomyResult,
)

METADATA_VERSION = "1"

# Status returned by merge_canonical_metadata.
MERGE_FILLED = "filled"
MERGE_UNCHANGED = "unchanged"
MERGE_CONFLICT = "conflict"
MERGE_UNAVAILABLE = "unavailable"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar(value: Any) -> Optional[str]:
    """Return a normalized scalar string, or ``None`` for unusable values."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _first_scalar(*values: Any) -> Optional[str]:
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


def normalize_source_trade_metadata(raw: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the exact PR66 metadata contract from an upstream-like mapping.

    Accepts either raw upstream fields (``eventId``, ``category``, ``series``)
    or an already canonical-shaped mapping. Never copies unknown fields.
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


def _official_category_for_v1_metadata(result: OfficialTaxonomyResult) -> Optional[str]:
    """Return only a conflict-free trusted broad category for metadata v1."""
    if result.status != TAXONOMY_USABLE:
        return None
    if result.source == "market.category":
        return result.market_category_value
    if result.source == "event.category":
        return result.event_category_value
    if result.source == "series.category":
        return result.series_category_value
    return result.category_label


def _as_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _parse_metadata(existing_json: Optional[str]) -> dict[str, Any]:
    if not existing_json:
        return {}
    try:
        parsed = json.loads(existing_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ensure_version(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``meta`` guaranteed to carry ``metadata_version``."""
    out = dict(meta)
    out["metadata_version"] = METADATA_VERSION
    return out


def build_canonical_metadata(
    trade: Optional[Mapping[str, Any]],
    gamma_market: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the canonical PR66 metadata dict from a trusted Gamma market.

    This is the shared producer: the existing approved-wallet collector's
    ``build_metadata_from_gamma_market`` delegates here, so collection,
    backfill, per-trade enrichment, and the approved-wallet collector emit a
    byte-identical nested shape (``metadata_version``, ``event``, ``taxonomy``,
    ``series``). The scorer reads ``metadata["taxonomy"]["raw_category"]``.

    SOLE AUTHORITY: the canonical taxonomy/event/series is derived ONLY from the
    trusted Gamma market. The ``trade`` argument is accepted for call-site
    compatibility but is NEVER used as a metadata source — we never read
    taxonomy, title, or question text from the raw trade row.

    Returns the exact PR66 shape. Deterministic (``sort_keys``) so byte-equivalent
    across call sites. Never infers taxonomy from title/question text.
    """
    market = _mapping(gamma_market)
    source = dict(market)
    events = market.get("events")
    if isinstance(events, list) and events:
        source["event"] = events[0]
    series = market.get("series")
    if isinstance(series, list) and series:
        source["series"] = series[0]
    source["category"] = _official_category_for_v1_metadata(
        OfficialPolymarketTaxonomyResolverV1().resolve(source)
    )
    return normalize_source_trade_metadata(source)


def _gamma_condition_id(gamma_market: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not gamma_market:
        return None
    cid = gamma_market.get("conditionId") or gamma_market.get("id")
    return str(cid).lower() if cid is not None else None


def _gamma_token_ids(gamma_market: Optional[Mapping[str, Any]]) -> set[str]:
    """Return the lower-cased token ids that belong to this Gamma condition."""
    if not gamma_market:
        return set()
    owned: set[str] = set()
    tokens = gamma_market.get("tokens")
    if isinstance(tokens, list):
        for tok in tokens:
            if isinstance(tok, Mapping):
                tid = tok.get("tokenId") or tok.get("token_id")
                if tid:
                    owned.add(str(tid).lower())
    return owned


def merge_canonical_metadata(
    existing_json: Optional[str],
    gamma_market: Optional[Mapping[str, Any]],
    *,
    condition_id: str,
    token_id: Optional[str] = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Safely merge canonical namespaces into existing ``metadata_json``.

    Returns ``(new_metadata, status, reason_codes)``.

    Rules:
      * Fills only canonical namespaces (``taxonomy``, ``event``, ``series``).
      * Preserves unrelated existing metadata (e.g. ``foo=bar``).
      * Never touches identity/economic columns.
      * SOLE authority for canonical taxonomy is the trusted Gamma market.
      * ``condition_id`` must EXACTLY match the Gamma market's condition id;
        mismatch -> ``unavailable`` (fail closed).
      * If the trade carries a ``token_id``, it must belong to the matched
        Gamma condition; explicit mismatch -> ``unavailable`` (fail closed).
      * Missing Gamma taxonomy -> ``unavailable`` (no error, no overwrite).
      * Any non-empty, differing value across taxonomy / event / series ->
        ``conflict`` (existing value preserved; caller must NOT overwrite).
      * Empty/missing existing fields are FILLABLE (filled from Gamma).
      * ``metadata_version`` is stamped on every returned shape.

    ``condition_id`` is the source_trade's ``market_source_id`` (the trusted
    Gamma condition id). The provided Gamma market MUST match it.
    """
    existing = _parse_metadata(existing_json)
    reason_codes: list[str] = []

    if gamma_market is None:
        reason_codes.append("gamma_missing")
        return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    g_cid = _gamma_condition_id(gamma_market)
    req_cid = condition_id.lower() if condition_id else None
    if g_cid != req_cid:
        reason_codes.append("condition_id_mismatch")
        return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    # Token ownership: a trade's token_id must belong to the matched condition.
    if token_id:
        owned = _gamma_token_ids(gamma_market)
        if owned and str(token_id).lower() not in owned:
            reason_codes.append("token_id_not_in_condition")
            return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    new = build_canonical_metadata({}, gamma_market)
    new_taxonomy = new.get("taxonomy") or {}
    if not new_taxonomy.get("raw_category"):
        reason_codes.append("taxonomy_unavailable")
        return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    merged = dict(existing)  # preserve unrelated metadata
    changed = False
    conflict = False

    for ns in ("taxonomy", "event", "series"):
        if ns not in new:
            continue
        existing_ns = existing.get(ns)
        new_ns = new[ns]
        if not isinstance(existing_ns, dict):
            merged[ns] = new_ns
            changed = True
            continue
        merged_ns = dict(existing_ns)
        for k, v in new_ns.items():
            if k not in existing_ns:
                # missing key in existing -> fillable
                merged_ns[k] = v
                changed = True
            elif k == "tags":
                # tags are an order-insensitive set: canonicalize both.
                ev_set = set(existing_ns[k]) if isinstance(existing_ns[k], list) else set()
                v_set = set(v) if isinstance(v, list) else set()
                if ev_set == v_set:
                    continue
                if not ev_set:
                    merged_ns[k] = v
                    changed = True
                else:
                    conflict = True
                    reason_codes.append("taxonomy_tags_conflict")
            else:
                ev = existing_ns[k]
                if ev == v:
                    # identical (incl. both None) -> no change
                    continue
                if ev is None or ev == "":
                    # existing is empty/None -> fillable from Gamma
                    merged_ns[k] = v
                    changed = True
                else:
                    # both non-empty and differing -> block (fail closed)
                    conflict = True
                    reason_codes.append(f"{ns}_{k}_conflict")
        if changed and not conflict:
            merged[ns] = merged_ns

    if conflict:
        return _ensure_version(existing), MERGE_CONFLICT, reason_codes

    if changed:
        merged = json.loads(_as_json(merged))
        merged["metadata_version"] = METADATA_VERSION
        return merged, MERGE_FILLED, reason_codes

    reason_codes.append("no_change")
    out = _ensure_version(existing)
    return out, MERGE_UNCHANGED, reason_codes
