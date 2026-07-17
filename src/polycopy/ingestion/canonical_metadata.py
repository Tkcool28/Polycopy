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
* Gamma markets are accepted as ``collections.abc.Mapping``. Trusted token
  membership is read from the real Gamma ``clobTokenIds`` field (JSON-encoded
  list string OR bare list) via the proven repository-wide
  ``parse_clob_token_ids`` contract — never a synthetic token dict.
* ``metadata_version`` is stamped on EVERY merge output (filled, unchanged, and
  preserved-existing shapes) so downstream consumers can detect drift. An
  existing non-empty version that differs from the canonical ``"1"`` is a
  version conflict (never silently rewritten).
* Conflicts are fail-closed: any non-empty, differing value across the
  taxonomy / event / series namespaces blocks the merge (status ``conflict``);
  the caller must NOT overwrite. Malformed / non-object existing metadata is
  likewise blocked (status ``unavailable``); the caller must preserve the
  original DB value (see ``merge_canonical_metadata`` return contract).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Optional

from polycopy.adapters.polymarket import parse_clob_token_ids
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


def _parse_metadata(existing_json: Optional[str]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse ``existing_json`` with fail-closed validation.

    Returns ``(parsed_dict, error_reason)``. ``parsed_dict`` is ``None`` and
    ``error_reason`` is set when the stored value is malformed JSON or a
    non-object top level. Empty / ``None`` input is valid and yields ``({}, None)``
    (an empty-but-valid existing row that may be safely filled).
    """
    if not existing_json:
        return {}, None
    try:
        parsed = json.loads(existing_json)
    except (json.JSONDecodeError, TypeError):
        return None, "existing_metadata_malformed_json"
    if not isinstance(parsed, dict):
        return None, "existing_metadata_not_object"
    return parsed, None


def _ensure_version(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``meta`` guaranteed to carry ``metadata_version``."""
    out = dict(meta)
    out["metadata_version"] = METADATA_VERSION
    return out


def _preserve_opt(existing_json: Optional[str]) -> dict[str, Any]:
    """Return the original stored value when it cannot be parsed.

    Callers pass this on ``unavailable``/``conflict`` so they can preserve the
    DB row verbatim. If the stored value is a string (even malformed), it is
    returned as-is; otherwise an empty dict is returned.
    """
    if isinstance(existing_json, str):
        return existing_json  # type: ignore[return-value]
    if existing_json is None:
        return {}
    return existing_json  # type: ignore[return-value]


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


def _gamma_token_ids(gamma_market: Optional[Mapping[str, Any]]) -> list[str]:
    """Return the token ids that belong to this Gamma condition.

    Uses the proven repository-wide ``parse_clob_token_ids`` contract, which
    accepts Gamma ``clobTokenIds`` as a JSON-encoded list string OR a bare
    list, and returns ``[]`` for missing/malformed/empty evidence. Tokens are
    normalized to lower-case for case-insensitive membership checks.
    """
    if not gamma_market:
        return []
    # parse_clob_token_ids reads ``clobTokenIds`` from the passed mapping; it
    # tolerates the field being absent or shaped unlike a list (returns []).
    tokens = parse_clob_token_ids(dict(gamma_market))
    return [str(t).lower() for t in tokens if t]


def merge_canonical_metadata(
    existing_json: Optional[str],
    gamma_market: Optional[Mapping[str, Any]],
    *,
    condition_id: str,
    token_id: Optional[str] = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Safely merge canonical namespaces into existing ``metadata_json``.

    Returns ``(new_metadata, status, reason_codes)``.

    RETURN CONTRACT (callers MUST honor this to preserve the original DB row):

      * status ``unavailable`` or ``conflict`` -> ``new_metadata`` is the
        ORIGINAL parsed existing object (or the raw ``existing_json`` string
        when it could not be parsed). Callers MUST NOT overwrite the stored
        row on these statuses — the original value is returned untouched.
      * status ``filled`` -> ``new_metadata`` is the merged object (byte-stable).
      * status ``unchanged`` -> ``new_metadata`` equals the original.

    Rules:
      * Fills only canonical namespaces (``taxonomy``, ``event``, ``series``).
      * Preserves unrelated existing metadata (e.g. ``foo=bar``).
      * Never touches identity/economic columns.
      * SOLE authority for canonical taxonomy is the trusted Gamma market.
      * ``condition_id`` must EXACTLY match the Gamma market's condition id;
        mismatch -> ``unavailable`` (fail closed).
      * If the trade carries a ``token_id``, it MUST belong to the matched
        Gamma condition per the real Gamma ``clobTokenIds`` list:
          - missing/malformed/empty Gamma token evidence -> ``unavailable``
            with ``token_membership_unavailable``
          - token absent from the parsed list -> ``unavailable`` with
            ``token_id_not_in_condition``
          - a duplicate/ambiguous occurrence -> ``unavailable`` with
            ``token_membership_ambiguous``
          - exactly one matching token proceeds.
      * Malformed / non-object existing metadata -> ``unavailable`` (blocked);
        the caller preserves the original DB value.
      * Missing Gamma taxonomy -> ``unavailable`` (no error, no overwrite).
      * ``metadata_version`` conflict: an existing non-empty version that
        differs from ``"1"`` blocks as ``version_conflict`` (never rewritten).
      * Any non-empty, differing value across taxonomy / event / series ->
        ``conflict`` (existing value preserved; caller must NOT overwrite).
      * Empty/missing existing fields are FILLABLE (filled from Gamma).
      * Incoming None/empty Gamma fields are absence of evidence: an existing
        populated value is PRESERVED (not a conflict).
      * ``metadata_version`` is stamped on every returned shape.

    ``condition_id`` is the source_trade's ``market_source_id`` (the trusted
    Gamma condition id). The provided Gamma market MUST match it.
    """
    # --- Fail-closed parse of the existing stored value. --------------------
    existing, parse_err = _parse_metadata(existing_json)
    if parse_err is not None:
        # Return the RAW original value so the caller can preserve it verbatim.
        return _preserve_opt(existing_json), MERGE_UNAVAILABLE, [parse_err]

    reason_codes: list[str] = []

    if gamma_market is None:
        reason_codes.append("gamma_missing")
        return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    g_cid = _gamma_condition_id(gamma_market)
    req_cid = condition_id.lower() if condition_id else None
    if g_cid != req_cid:
        reason_codes.append("condition_id_mismatch")
        return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    # --- Token ownership via the REAL Gamma token contract. ----------------
    if token_id:
        owned = _gamma_token_ids(gamma_market)
        if not owned:
            reason_codes.append("token_membership_unavailable")
            return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes
        match_count = sum(1 for t in owned if t == str(token_id).lower())
        if match_count == 0:
            reason_codes.append("token_id_not_in_condition")
            return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes
        if match_count > 1:
            reason_codes.append("token_membership_ambiguous")
            return _ensure_version(existing), MERGE_UNAVAILABLE, reason_codes

    # --- metadata_version conflict (before building the merge). ------------
    existing_version = existing.get("metadata_version")
    if existing_version not in (None, "", METADATA_VERSION):
        reason_codes.append("version_conflict")
        # Preserve the original (do NOT rewrite version "2" -> "1").
        return dict(existing), MERGE_CONFLICT, reason_codes

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
            # empty / None existing namespace -> fillable;
            # a NON-EMPTY non-dict existing namespace -> conflict.
            if existing_ns is None or (isinstance(existing_ns, (str, list, tuple, set, dict)) and not existing_ns):
                merged[ns] = new_ns
                changed = True
            else:
                conflict = True
                reason_codes.append(f"{ns}_not_dict_conflict")
            continue
        merged_ns = dict(existing_ns)
        for k, v in new_ns.items():
            if v is None or v == "" or (isinstance(v, (list, tuple, set, dict)) and not v):
                # incoming Gamma field is absence of evidence (None, empty
                # string, or empty collection): preserve an existing populated
                # value; never a conflict and never a fill.
                continue
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
                elif not isinstance(ev, (str, int, float, bool)) or not isinstance(
                    v, (str, int, float, bool)
                ):
                    # a non-empty namespace field of the wrong type -> conflict
                    conflict = True
                    reason_codes.append(f"{ns}_{k}_type_conflict")
                else:
                    # both non-empty scalars and differing -> block (fail closed)
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
