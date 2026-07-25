"""Canonical market-evidence metadata for source-trade ingestion.

Gamma is authoritative for market identity, taxonomy, outcomes, lifecycle,
resolution, and provider update time. Wallet-trade fields are context only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Optional

from polycopy.adapters.polymarket import parse_clob_token_ids
from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_USABLE,
    OfficialPolymarketTaxonomyResolverV1,
    OfficialTaxonomyResult,
)

METADATA_VERSION = "1"
MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION = "2"

MERGE_FILLED = "filled"
MERGE_UNCHANGED = "unchanged"
MERGE_CONFLICT = "conflict"
MERGE_UNAVAILABLE = "unavailable"

_IMMUTABLE_PROVENANCE_FIELDS = frozenset(
    {
        "provider",
        "lookup_kind",
        "requested_condition_id",
        "exact_match",
        "snapshot_contract_version",
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar(value: Any) -> Optional[str]:
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
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    normalized = {_scalar(item) for item in value}
    return sorted(tag for tag in normalized if tag is not None)


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (
        isinstance(value, (list, tuple, set, frozenset, dict)) and not value
    )


def _ensure_version(meta: dict[str, Any]) -> dict[str, Any]:
    out = dict(meta)
    out["metadata_version"] = METADATA_VERSION
    return out


def _parse_array(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _validate_outcome_mapping(
    labels_raw: Any,
    clob_raw: Any,
    outcome_index: Any = None,
    selected_token: str | None = None,
    selected_outcome: str | None = None,
) -> dict[str, Any]:
    """Return a complete ordered mapping only when every consistency check passes."""
    errors: list[str] = []
    raw_labels = _parse_array(labels_raw)
    raw_tokens = _parse_array(clob_raw)

    labels: list[str] = []
    if raw_labels is not None:
        for index, value in enumerate(raw_labels):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"blank_or_invalid_label_at_index={index}")
                labels.append("")
            else:
                labels.append(value.strip())

    token_ids: list[str] = []
    if raw_tokens is not None:
        for index, value in enumerate(raw_tokens):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"blank_or_invalid_token_at_index={index}")
                token_ids.append("")
            else:
                token_ids.append(value.strip().lower())

    if not labels:
        errors.append("labels_missing_or_empty")
    if not token_ids:
        errors.append("token_ids_missing_or_empty")
    if labels and token_ids and len(labels) != len(token_ids):
        errors.append("array_length_mismatch")
    usable_tokens = [token for token in token_ids if token]
    if len(usable_tokens) != len(set(usable_tokens)):
        errors.append("duplicate_token_ids")

    valid_index: bool | None = None
    index_token_agrees: bool | None = None
    index_outcome_agrees: Optional[bool] = None
    if outcome_index is not None:
        valid_index = (
            isinstance(outcome_index, int)
            and not isinstance(outcome_index, bool)
            and 0 <= outcome_index < len(labels)
            and outcome_index < len(token_ids)
        )
        if not valid_index:
            errors.append(f"outcome_index_out_of_range={outcome_index}")
        else:
            if selected_token is not None:
                index_token_agrees = token_ids[outcome_index] == str(selected_token).strip().lower()
                if not index_token_agrees:
                    errors.append("index_token_disagreement")
            if selected_outcome is not None:
                index_outcome_agrees = (
                    labels[outcome_index].casefold() == str(selected_outcome).strip().casefold()
                )
                if not index_outcome_agrees:
                    errors.append("index_outcome_disagreement")
    else:
        normalized_token = (
            selected_token.strip().lower()
            if isinstance(selected_token, str) and selected_token.strip()
            else None
        )
        normalized_outcome = (
            selected_outcome.strip().casefold()
            if isinstance(selected_outcome, str) and selected_outcome.strip()
            else None
        )
        token_matches = [
            index for index, token in enumerate(token_ids) if token == normalized_token
        ]
        outcome_matches = [
            index
            for index, label in enumerate(labels)
            if label.casefold() == normalized_outcome
        ]
        if normalized_token is not None and len(token_matches) != 1:
            errors.append("selected_token_not_in_mapping")
        if normalized_outcome is not None and not outcome_matches:
            errors.append("selected_outcome_not_in_mapping")
        if (
            normalized_token is not None
            and normalized_outcome is not None
            and not set(token_matches).intersection(outcome_matches)
        ):
            errors.append("selected_token_outcome_disagreement")

    compatible = not errors
    if compatible:
        status = "complete"
        ordered = [
            {"label": label, "clob_token_id": token_ids[index]}
            for index, label in enumerate(labels)
        ]
    else:
        status = "invalid" if labels and token_ids else "incomplete"
        ordered = []

    return {
        "labels": labels,
        "token_ids": token_ids,
        "ordered": ordered,
        "compatible": compatible,
        "valid_index": valid_index,
        "index_token_agrees": index_token_agrees,
        "index_outcome_agrees": index_outcome_agrees,
        "status": status,
        "errors": errors,
    }


def _winner_candidates(market: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    token_fields = ("winnerTokenId", "winningClobTokenId")
    outcome_fields = ("winnerOutcome", "resolutionOutcome")
    tokens: list[str] = []
    outcomes: list[str] = []
    sources: list[str] = []

    containers: list[tuple[str, Mapping[str, Any]]] = [("market", market)]
    resolution = market.get("resolution")
    if isinstance(resolution, Mapping):
        containers.append(("market.resolution", resolution))

    for prefix, container in containers:
        for field in token_fields:
            value = container.get(field)
            values = value if isinstance(value, list) else [value]
            for item in values:
                normalized = _scalar(item)
                if normalized is not None:
                    tokens.append(normalized.lower())
                    sources.append(f"{prefix}.{field}")
        for field in outcome_fields:
            value = container.get(field)
            values = value if isinstance(value, list) else [value]
            for item in values:
                normalized = _scalar(item)
                if normalized is not None:
                    outcomes.append(normalized)
                    sources.append(f"{prefix}.{field}")
    return tokens, outcomes, sources


def _parse_resolution_fields(
    market: Mapping[str, Any], outcomes: Mapping[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "resolution_status": None,
        "winner_token_id": None,
        "winner_outcome": None,
        "evidence_fields": [],
        "errors": [],
    }
    if market.get("resolved") is not True:
        return result

    tokens, labels, fields = _winner_candidates(market)
    unique_tokens = list(dict.fromkeys(tokens))
    unique_labels = list(dict.fromkeys(labels))
    result["evidence_fields"] = sorted(set(fields))

    if len(unique_tokens) > 1 or len(unique_labels) > 1:
        result["resolution_status"] = "invalid"
        result["errors"].append("multiple_or_contradictory_winners")
        return result

    pairs = outcomes.get("ordered") if outcomes.get("compatible") else []
    token = unique_tokens[0] if unique_tokens else None
    label = unique_labels[0] if unique_labels else None

    if token is not None and label is None:
        matches = [pair["label"] for pair in pairs if pair["clob_token_id"] == token]
        label = matches[0] if len(matches) == 1 else None
    if label is not None and token is None:
        matches = [
            pair["clob_token_id"]
            for pair in pairs
            if pair["label"].casefold() == label.casefold()
        ]
        token = matches[0] if len(matches) == 1 else None

    if token is None or label is None:
        result["resolution_status"] = "incomplete"
        result["errors"].append("resolved_without_derivable_winner")
        return result

    matching_pairs = [
        pair
        for pair in pairs
        if pair["clob_token_id"] == token
        and pair["label"].casefold() == label.casefold()
    ]
    if token is not None and label is not None and len(matching_pairs) != 1:
        result["resolution_status"] = "invalid"
        result["errors"].append("winner_token_outcome_disagreement")
        return result

    result.update(
        resolution_status="resolved",
        winner_token_id=token,
        winner_outcome=label,
    )
    return result


def normalize_source_trade_metadata(raw: Optional[Mapping[str, Any]]) -> dict[str, Any]:
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
            "tags": _tags(
                taxonomy.get("tags") if "tags" in taxonomy else source.get("tags")
            ),
        },
        "series": {
            "id": _first_scalar(series.get("id"), source.get("seriesId")),
            "slug": _first_scalar(series.get("slug"), source.get("seriesSlug")),
            "title": _first_scalar(series.get("title"), source.get("seriesTitle")),
            "ticker": _first_scalar(series.get("ticker"), source.get("ticker")),
        },
    }


def _official_category_for_v1_metadata(result: OfficialTaxonomyResult) -> Optional[str]:
    if result.status != TAXONOMY_USABLE:
        return None
    if result.source == "market.category":
        return result.market_category_value
    if result.source == "event.category":
        return result.event_category_value
    if result.source == "series.category":
        return result.series_category_value
    return result.category_label


def _trade_index(trade: Optional[Mapping[str, Any]]) -> Optional[int]:
    if not trade or "outcomeIndex" not in trade:
        return None
    value = trade.get("outcomeIndex")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_market_snapshot(
    market: Mapping[str, Any], trade: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    outcomes = _validate_outcome_mapping(
        market.get("outcomes"),
        market.get("clobTokenIds"),
        outcome_index=trade.get("outcomeIndex") if trade else None,
        selected_token=_first_scalar(
            trade.get("asset") if trade else None,
            trade.get("token_id") if trade else None,
        ),
        selected_outcome=_scalar(trade.get("outcome")) if trade else None,
    )
    snapshot: dict[str, Any] = {
        "market": {
            "condition_id": _scalar(market.get("conditionId")),
            "provider_market_id": _scalar(market.get("id")),
            "question": _scalar(market.get("question")),
            "slug": _scalar(market.get("slug")),
        },
        "outcomes": outcomes,
        "lifecycle": {
            "active": _as_bool(market.get("active")),
            "closed": _as_bool(market.get("closed")),
            "accepting_orders": _as_bool(market.get("acceptingOrders")),
            "end_date": _scalar(market.get("endDate")),
        },
        "resolution": _parse_resolution_fields(market, outcomes),
        "provenance": {
            "provider": "gamma",
            "lookup_kind": "condition_id",
            "requested_condition_id": _scalar(market.get("conditionId")),
            "exact_match": True,
            "snapshot_contract_version": MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION,
        },
    }
    provider_updated_at = _scalar(market.get("updatedAt"))
    if provider_updated_at is not None:
        snapshot["provenance"]["provider_updated_at"] = provider_updated_at
    if trade:
        provenance = snapshot["provenance"]
        context = {
            "trade_response_title": _scalar(trade.get("title")),
            "trade_response_slug": _scalar(trade.get("slug")),
            "trade_response_outcome_index": _trade_index(trade),
            "trade_response_transaction_hash": _scalar(trade.get("transactionHash")),
        }
        provenance.update({key: value for key, value in context.items() if value is not None})
    return snapshot


def build_canonical_metadata(
    trade: Optional[Mapping[str, Any]], gamma_market: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
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
    result = normalize_source_trade_metadata(source)
    if market:
        result["_snapshot"] = _build_market_snapshot(market, trade)
    return result


def _gamma_condition_id(gamma_market: Mapping[str, Any]) -> Optional[str]:
    value = gamma_market.get("conditionId")
    return str(value).lower() if value is not None else None


def _gamma_token_ids(gamma_market: Mapping[str, Any]) -> list[str]:
    return [str(token).lower() for token in parse_clob_token_ids(dict(gamma_market)) if token]


def _merge_standard_namespace(
    existing: dict[str, Any], incoming: dict[str, Any], namespace: str
) -> tuple[dict[str, Any], bool, list[str]]:
    current = existing.get(namespace)
    reasons: list[str] = []
    if not isinstance(current, dict):
        if _is_empty(current):
            return dict(incoming), True, reasons
        return {}, False, [f"{namespace}_not_dict_conflict"]

    output = dict(current)
    changed = False
    for key, value in incoming.items():
        if _is_empty(value):
            continue
        old = current.get(key)
        if _is_empty(old):
            output[key] = value
            changed = True
            continue
        if key == "tags":
            if not isinstance(old, (list, tuple, set, frozenset)):
                reasons.append(f"{namespace}_{key}_type_conflict")
                continue
            if set(_tags(old)) == set(_tags(value)):
                continue
        elif old == value:
            continue
        reasons.append(f"{namespace}_{key}_conflict")
    return output, changed, reasons


def _merge_refreshable_dict(
    current: Any,
    incoming: Mapping[str, Any],
    namespace: str,
    *,
    immutable_fields: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], bool, list[str]]:
    if not isinstance(current, dict):
        if _is_empty(current):
            return dict(incoming), True, []
        return {}, False, [f"_snapshot_{namespace}_type_conflict"]
    output = dict(current)
    changed = False
    reasons: list[str] = []
    for key, value in incoming.items():
        if _is_empty(value):
            continue
        old = current.get(key)
        if _is_empty(old):
            output[key] = value
            changed = True
        elif old == value:
            continue
        elif (
            key in immutable_fields
            or isinstance(old, (dict, list))
            or isinstance(value, (dict, list))
        ):
            reasons.append(f"_snapshot_{namespace}_{key}_conflict")
        else:
            output[key] = value
            changed = True
    return output, changed, reasons


def _merge_snapshot(
    existing_snapshot: Any, incoming: Mapping[str, Any]
) -> tuple[dict[str, Any], bool, list[str]]:
    if not isinstance(existing_snapshot, dict):
        if _is_empty(existing_snapshot):
            return dict(incoming), True, []
        return {}, False, ["_snapshot_type_conflict"]

    output = dict(existing_snapshot)
    changed = False
    reasons: list[str] = []
    for namespace in ("market", "outcomes", "lifecycle", "resolution", "provenance"):
        new_namespace = incoming.get(namespace)
        if not isinstance(new_namespace, Mapping):
            continue
        merged_namespace, namespace_changed, namespace_reasons = _merge_refreshable_dict(
            existing_snapshot.get(namespace),
            new_namespace,
            namespace,
            immutable_fields=(
                _IMMUTABLE_PROVENANCE_FIELDS
                if namespace == "provenance"
                else frozenset()
            ),
        )
        if namespace_changed:
            output[namespace] = merged_namespace
            changed = True
        reasons.extend(namespace_reasons)
    return output, changed, reasons


def _snapshot_for_replay_comparison(snapshot: Any) -> Any:
    """Remove audit-only timestamps before substantive replay comparison."""
    if not isinstance(snapshot, dict):
        return snapshot
    output = json.loads(json.dumps(snapshot))
    provenance = output.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("retrieved_at", None)
        provenance.pop("provider_updated_at", None)
    return output


def merge_canonical_metadata(
    existing_json: Optional[str],
    gamma_market: Optional[Mapping[str, Any]],
    *,
    condition_id: str,
    token_id: Optional[str] = None,
) -> tuple[dict[str, Any], str, list[str]]:
    if not existing_json:
        existing: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(existing_json)
        except (json.JSONDecodeError, TypeError):
            return existing_json, MERGE_UNAVAILABLE, ["existing_metadata_malformed_json"]  # type: ignore[return-value]
        if not isinstance(parsed, dict):
            return existing_json, MERGE_UNAVAILABLE, ["existing_metadata_not_object"]  # type: ignore[return-value]
        existing = parsed

    if gamma_market is None:
        return _ensure_version(existing), MERGE_UNAVAILABLE, ["gamma_missing"]
    if _gamma_condition_id(gamma_market) != condition_id.lower():
        return _ensure_version(existing), MERGE_UNAVAILABLE, ["condition_id_mismatch"]
    if token_id:
        owned = _gamma_token_ids(gamma_market)
        matches = sum(token == str(token_id).lower() for token in owned)
        if not owned:
            return _ensure_version(existing), MERGE_UNAVAILABLE, ["token_membership_unavailable"]
        if matches == 0:
            return _ensure_version(existing), MERGE_UNAVAILABLE, ["token_id_not_in_condition"]
        if matches > 1:
            return _ensure_version(existing), MERGE_UNAVAILABLE, ["token_membership_ambiguous"]

    version = existing.get("metadata_version")
    if version not in (None, "", METADATA_VERSION):
        return dict(existing), MERGE_CONFLICT, ["version_conflict"]

    incoming = build_canonical_metadata({}, gamma_market)
    merged = dict(existing)
    changed = False
    reasons: list[str] = []
    existing_snapshot = existing.get("_snapshot")
    incoming_snapshot = incoming["_snapshot"]
    snapshot_substantively_equal = (
        _snapshot_for_replay_comparison(existing_snapshot)
        == _snapshot_for_replay_comparison(incoming_snapshot)
    )

    for namespace in ("taxonomy", "event", "series"):
        merged_namespace, namespace_changed, namespace_reasons = _merge_standard_namespace(
            existing, incoming[namespace], namespace
        )
        if namespace_changed:
            merged[namespace] = merged_namespace
            changed = True
        reasons.extend(namespace_reasons)

    if snapshot_substantively_equal:
        snapshot = existing_snapshot
        snapshot_changed = False
        snapshot_reasons: list[str] = []
    else:
        snapshot, snapshot_changed, snapshot_reasons = _merge_snapshot(
            existing_snapshot, incoming_snapshot
        )
    if snapshot_changed:
        assert isinstance(snapshot, dict)
        provenance = snapshot.setdefault("provenance", {})
        provenance["retrieved_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        merged["_snapshot"] = snapshot
        changed = True
    reasons.extend(snapshot_reasons)

    merged["metadata_version"] = METADATA_VERSION
    if reasons:
        return json.loads(json.dumps(merged, sort_keys=True)), MERGE_CONFLICT, reasons
    if not changed:
        reasons.append("no_change")
        return _ensure_version(existing), MERGE_UNCHANGED, reasons
    return json.loads(json.dumps(merged, sort_keys=True)), MERGE_FILLED, reasons


def build_metadata_from_gamma_market(
    raw: Optional[Mapping[str, Any]],
    gamma_market: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compatibility alias for callers that have not moved to the canonical name."""
    return build_canonical_metadata(raw, gamma_market)
