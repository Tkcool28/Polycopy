"""Canonical market-evidence metadata for source-trade ingestion.

Gamma is authoritative for market identity, taxonomy, outcomes, lifecycle,
resolution, and provider update time. Wallet-trade fields are context only.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, final

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


_CANONICAL_TRUST_TOKEN = object()


@final
class CanonicalSourceTradeMetadata(dict[str, Any]):
    """Trusted dictionary created only by the canonical builder.

    Construction requires a module-private identity token and snapshots the
    builder-owned dictionary. Exact type identity selects trusted serialization;
    ordinary dicts and all subclasses remain untrusted. ``deepcopy`` deliberately
    returns a plain dict so copied payloads lose trust.
    """

    def __init__(self, value: dict[str, Any], *, _token: object) -> None:
        if _token is not _CANONICAL_TRUST_TOKEN:
            raise TypeError("CanonicalSourceTradeMetadata is builder-only")
        super().__init__(copy.deepcopy(value))

    def __deepcopy__(self, memo: dict[int, Any]) -> "CanonicalSourceTradeMetadata":
        return CanonicalSourceTradeMetadata(
            copy.deepcopy(dict(self), memo), _token=_CANONICAL_TRUST_TOKEN
        )

    def to_plain_dict(self) -> dict[str, Any]:
        """Return an untrusted deep copy for inspection or legacy handling."""
        return copy.deepcopy(dict(self))


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


def _scalar(value: Any) -> str | None:
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
    """Return a complete ordered mapping only when every consistency check passes.

    The result separates two error streams so the snapshot can distinguish
    authoritative Gamma-shape evidence from optional source-trade context:

    * ``errors`` — substantive Gamma-shape diagnostics (label blanks, token
      blanks, missing labels, missing tokens, array-length mismatch,
      duplicate token IDs). These stay inside ``_snapshot.outcomes`` and
      are compared during substantive replay.
    * ``trade_validation`` — caller-context diagnostics (outcome-index range,
      index / token agreement, index / outcome agreement, selected-token
      membership, selected-outcome membership, selected token-outcome
      disagreement). These depend on the optional trade fields the caller
      supplied (selected_token / selected_outcome / outcome_index) and the
      value of the booleans (``valid_index`` / ``index_token_agrees`` /
      ``index_outcome_agrees``) likewise depend on that context. They are
      returned on the top level too for backward compatibility but the
      builder routes them into ``_snapshot.provenance.trade_validation``
      so that two otherwise identical Gamma responses produce substantively
      equivalent authoritative evidence regardless of caller context.
    """
    errors: list[str] = []
    trade_validation_errors: list[str] = []
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
    index_outcome_agrees: bool | None = None
    if outcome_index is not None:
        valid_index = (
            isinstance(outcome_index, int)
            and not isinstance(outcome_index, bool)
            and 0 <= outcome_index < len(labels)
            and outcome_index < len(token_ids)
        )
        if not valid_index:
            trade_validation_errors.append(
                f"outcome_index_out_of_range={outcome_index}"
            )
        else:
            if selected_token is not None:
                index_token_agrees = (
                    token_ids[outcome_index]
                    == str(selected_token).strip().lower()
                )
                if not index_token_agrees:
                    trade_validation_errors.append("index_token_disagreement")
            if selected_outcome is not None:
                index_outcome_agrees = (
                    labels[outcome_index].casefold()
                    == str(selected_outcome).strip().casefold()
                )
                if not index_outcome_agrees:
                    trade_validation_errors.append("index_outcome_disagreement")
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
            trade_validation_errors.append("selected_token_not_in_mapping")
        if normalized_outcome is not None and not outcome_matches:
            trade_validation_errors.append("selected_outcome_not_in_mapping")
        if (
            normalized_token is not None
            and normalized_outcome is not None
            and not set(token_matches).intersection(outcome_matches)
        ):
            trade_validation_errors.append("selected_token_outcome_disagreement")

    # The authoritative outcome mapping is "Gamma-compatible" only when the
    # Gamma-shape errors list is empty. The trade-context diagnostics are
    # informational and never gate ``compatible`` / ``status`` / ``ordered``.
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
        "trade_validation_errors": trade_validation_errors,
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


def normalize_source_trade_metadata(raw: Mapping[str, Any] | None) -> dict[str, Any]:
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


def _official_category_for_v1_metadata(result: OfficialTaxonomyResult) -> str | None:
    if result.status != TAXONOMY_USABLE:
        return None
    if result.source == "market.category":
        return result.market_category_value
    if result.source == "event.category":
        return result.event_category_value
    if result.source == "series.category":
        return result.series_category_value
    return result.category_label


def _strict_trade_index_value(value: Any) -> int | None:
    """Return ``value`` if it is a strict integer index; otherwise ``None``.

    Accepts ONLY a real Python ``int`` that is non-negative and not a
    boolean. Floats (including integral-looking ``0.0``), bools, decimal
    strings, scientific-notation strings, empty strings, negative integers,
    ``None`` and any other non-int shape are rejected. The function never
    coerces fractional or ambiguous values; an absent or unusable value
    yields ``None`` and is therefore omitted from any persisted provenance.

    This is the SINGLE strict outcome-index parser used by both the outcome
    mapping validation and the persisted provenance field; the two consumers
    cannot disagree on what counts as a valid index.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _trade_index(trade: Mapping[str, Any] | None) -> int | None:
    if not trade or "outcomeIndex" not in trade:
        return None
    return _strict_trade_index_value(trade.get("outcomeIndex"))


def _build_market_snapshot(
    market: Mapping[str, Any],
    trade: Mapping[str, Any] | None,
    *,
    requested_condition_id: str | None = None,
    enforce_exact_condition_match: bool = False,
) -> dict[str, Any]:
    """Build the canonical market-evidence snapshot for one trade + Gamma.

    ``enforce_exact_condition_match`` is the initial-ingestion safety gate:
    when True, the snapshot's ``exact_match`` is False unless the caller's
    ``requested_condition_id`` is present and matches the Gamma market's
    ``conditionId`` after canonical normalization. When False (legacy
    backfill contract), ``exact_match`` reflects whether the Gamma market
    itself carries a condition id — never an unknown trade's requested
    condition id (the merge layer enforces identity upstream).

    The authoritative Gamma-shape evidence lives in ``snapshot.outcomes``
    (its ``errors`` list, ``labels`` / ``token_ids`` / ``ordered``
    / ``compatible`` / ``status``). Trade-context validation diagnostics
    — outcome-index range, index/token agreement, index/outcome agreement,
    selected token/outcome membership and disagreement, plus the matching
    booleans (``valid_index`` / ``index_token_agrees`` /
    ``index_outcome_agrees``) — are split out into
    ``snapshot.provenance.trade_validation`` so that two otherwise
    identical Gamma responses produce substantively equivalent
    authoritative evidence regardless of whether the caller supplied
    trade context. The booleans are emitted under
    ``trade_validation`` together with their error list. The validator's
    Gamma-shape ``errors`` list is never polluted by caller context.
    """
    outcomes = _validate_outcome_mapping(
        market.get("outcomes"),
        market.get("clobTokenIds"),
        outcome_index=_strict_trade_index_value(
            trade.get("outcomeIndex")
        )
        if trade
        else None,
        selected_token=_first_scalar(
            trade.get("asset") if trade else None,
            trade.get("token_id") if trade else None,
        ),
        selected_outcome=_scalar(trade.get("outcome")) if trade else None,
    )
    # Capture the trade-validation diagnostics before the authoritative
    # ``outcomes`` namespace is rewritten — these are no longer part of
    # the authoritative Gamma-shape evidence.
    trade_validation: dict[str, Any] = {
        "errors": list(outcomes.pop("trade_validation_errors", []) or []),
        "valid_index": outcomes.get("valid_index"),
        "index_token_agrees": outcomes.get("index_token_agrees"),
        "index_outcome_agrees": outcomes.get("index_outcome_agrees"),
        "outcome_index_supplied": bool(
            trade is not None
            and "outcomeIndex" in trade
            and _strict_trade_index_value(trade.get("outcomeIndex")) is not None
        ),
    }
    # Strip the trade-context booleans from ``outcomes`` so they live ONLY
    # under ``provenance.trade_validation``. ``outcomes`` is now purely the
    # authoritative Gamma-shape evidence.
    for key in ("valid_index", "index_token_agrees", "index_outcome_agrees"):
        outcomes.pop(key, None)
    normalized_gamma_condition_id = _normalize_condition_id(market.get("conditionId"))
    if enforce_exact_condition_match:
        # Initial-ingestion contract: ``requested_condition_id`` MUST be
        # present and match the Gamma market's conditionId after
        # normalization. Any other shape means we cannot prove identity, so
        # ``exact_match=False`` and authoritative evidence is cleared.
        if (
            requested_condition_id is None
            or not _exact_condition_match(requested_condition_id, market)
        ):
            requested_for_provenance = requested_condition_id
            exact_match = False
        else:
            requested_for_provenance = requested_condition_id
            exact_match = True
    else:
        # Legacy backfill contract: identity has already been verified
        # upstream by ``merge_canonical_metadata``. ``exact_match`` simply
        # reflects whether the Gamma market carries a conditionId.
        requested_for_provenance = normalized_gamma_condition_id
        exact_match = requested_for_provenance is not None
    snapshot: dict[str, Any] = {
        "market": {
            "condition_id": normalized_gamma_condition_id,
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
            "requested_condition_id": requested_for_provenance,
            "exact_match": exact_match,
            "snapshot_contract_version": MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION,
        },
    }
    if not exact_match:
        # Fail-closed: a Gamma mapping without a verified condition-id
        # match MUST NOT carry authoritative evidence. Clear the populated
        # namespaces so we never silently stamp ``exact_match=False`` while
        # still persisting the mapping as authoritative. Provenance is
        # preserved so callers can see exactly why.
        snapshot["market"] = {key: None for key in snapshot["market"]}
        snapshot["lifecycle"] = {key: None for key in snapshot["lifecycle"]}
        snapshot["outcomes"] = {
            "labels": [],
            "token_ids": [],
            "ordered": [],
            "compatible": False,
            "status": "invalid",
            "errors": ["exact_match_false_evidence_unavailable"],
        }
        snapshot["resolution"] = {
            "resolution_status": None,
            "winner_token_id": None,
            "winner_outcome": None,
            "evidence_fields": [],
            "errors": ["exact_match_false_evidence_unavailable"],
        }
    provider_updated_at = _scalar(market.get("updatedAt"))
    if provider_updated_at is not None:
        snapshot["provenance"]["provider_updated_at"] = provider_updated_at
    # Trade-context provenance: wallet fields live at the top of
    # ``provenance``; trade-validation diagnostics live under a nested
    # object so they survive the deterministic JSON contract and are
    # never confused with substantive Gamma-shape errors. An absent
    # trade-context produces a minimal ``trade_validation`` namespace
    # with an empty errors list and ``outcome_index_supplied=False``.
    snapshot["provenance"]["trade_validation"] = trade_validation
    if trade:
        provenance = snapshot["provenance"]
        context = {
            "trade_response_title": _scalar(trade.get("title")),
            "trade_response_slug": _scalar(trade.get("slug")),
            "trade_response_outcome_index": _strict_trade_index_value(
                trade.get("outcomeIndex")
            ),
            "trade_response_transaction_hash": _scalar(trade.get("transactionHash")),
        }
        provenance.update({key: value for key, value in context.items() if value is not None})
    return snapshot


def build_canonical_metadata(
    trade: Mapping[str, Any] | None,
    gamma_market: Mapping[str, Any] | None,
    *,
    requested_condition_id: str | None = None,
    enforce_exact_condition_match: bool = False,
) -> dict[str, Any] | CanonicalSourceTradeMetadata:
    """Build the canonical PR66 metadata payload from a trusted Gamma market.

    Parameters
    ----------
    trade:
        Raw upstream trade dict, or ``None`` for metadata-only call sites.
    gamma_market:
        Raw Gamma market dict, or ``None`` for metadata-only call sites.
    requested_condition_id:
        The trade's requested condition id after canonical normalization.
        When ``None`` the function derives it from ``trade`` (using the
        ``conditionId`` or ``market_source_id`` field). Initial-ingestion
        callers pass the value explicitly to drive the new gate.
    enforce_exact_condition_match:
        When True, the snapshot is ONLY emitted when the canonical requested
        condition id matches the Gamma market's ``conditionId`` after
        normalization. When False (default — backfill semantics), the
        snapshot is always emitted and ``exact_match`` reflects whether the
        Gamma market itself carries a condition id. Initial-ingestion
        callers pass ``True`` to enforce the safety gate.
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
    result = normalize_source_trade_metadata(source)
    if not market:
        return result
    if requested_condition_id is None:
        requested_condition_id = _requested_condition_id(trade)
    if enforce_exact_condition_match:
        # Initial-ingestion safety gate: a requested condition id MUST
        # normalize-match the Gamma market's ``conditionId``; otherwise we
        # drop the snapshot entirely so the persisted row never stamps
        # ``exact_match=false`` with authoritative evidence attached. We
        # also refuse to enrich the v1 (event/taxonomy/series) namespaces
        # from the mismatched Gamma so no Gamma-authoritative field leaks
        # into the persisted row at all.
        if requested_condition_id is None:
            return normalize_source_trade_metadata(trade)
        if not _exact_condition_match(requested_condition_id, market):
            return normalize_source_trade_metadata(trade)
    result["_snapshot"] = _build_market_snapshot(
        market,
        trade,
        requested_condition_id=requested_condition_id,
        enforce_exact_condition_match=enforce_exact_condition_match,
    )
    return CanonicalSourceTradeMetadata(result, _token=_CANONICAL_TRUST_TOKEN)


def _gamma_condition_id(gamma_market: Mapping[str, Any]) -> str | None:
    value = gamma_market.get("conditionId")
    return str(value).lower() if value is not None else None


def _gamma_token_ids(gamma_market: Mapping[str, Any]) -> list[str]:
    return [str(token).lower() for token in parse_clob_token_ids(dict(gamma_market)) if token]


def _normalize_condition_id(value: Any) -> str | None:
    """Return a lowercased canonical condition-id string, or ``None`` for empty.

    Whitespace is stripped and the value must be a non-empty string. Any
    non-string (or empty after strip) yields ``None`` so the comparison is
    fail-closed: only normalized non-empty strings can prove an identity
    match. This is the single condition-id normalization rule for both the
    backfill merge path and the initial-ingestion canonical builder.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    return stripped or None


def _requested_condition_id(trade: Mapping[str, Any] | None) -> str | None:
    """Return the requested trade condition id, normalized.

    Accepts the raw ``conditionId`` field or, for DB-row-shaped inputs, the
    ``market_source_id`` field. Any other value yields ``None`` so the
    canonical builder cannot infer identity from any secondary field.
    """
    if not isinstance(trade, Mapping):
        return None
    raw = trade.get("conditionId")
    if raw is None:
        raw = trade.get("market_source_id")
    return _normalize_condition_id(raw)


def _exact_condition_match(
    requested: str | None, gamma_market: Mapping[str, Any]
) -> bool:
    """Return True iff requested and gamma condition ids match after normalization.

    A normalized requested condition id AND a gamma condition id must both
    be present and byte-equal after strip/lower. Empty or missing either
    side fails closed. The function is intentionally pure: it never infers
    identity from any other Gamma field (slug, title, question, token).
    """
    return (
        requested is not None
        and _normalize_condition_id(gamma_market.get("conditionId")) == requested
    )


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
            continue
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

def _merge_trade_validation(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Union-merge two ``provenance.trade_validation`` payloads.

    The diagnostics are caller-context only: an earlier build may have
    observed trade-context errors (selected token/outcome disagreement) that
    a later no-context build cannot observe. The audit demands:

      * existing diagnostics are preserved through replay;
      * new diagnostics from a later trade-context build are merged in;
      * contradictory context does NOT silently overwrite accepted
        diagnostics;
      * null / missing context does NOT erase existing diagnostics.

    Return the merged dict, or ``existing`` unchanged when the merge is a
    no-op (callers rely on identity to skip downstream writes).
    """
    if not isinstance(incoming, dict):
        return existing
    merged = dict(existing)
    # ``errors`` is the union, preserving insertion order so the deterministic
    # JSON contract stays stable. Already-present diagnostics are never
    # overwritten.
    existing_errors = list(existing.get("errors") or [])
    incoming_errors = list(incoming.get("errors") or [])
    seen = set(existing_errors)
    for item in incoming_errors:
        if item not in seen:
            existing_errors.append(item)
            seen.add(item)
    if existing_errors != list(existing.get("errors") or []):
        merged["errors"] = existing_errors
    # Booleans: if the existing context already observed a value, keep it;
    # only fill when the existing value is ``None`` and the incoming value is
    # informative. This protects against contradictory later context silently
    # overwriting accepted diagnostics; a later ``None`` never clears an
    # already-observed True/False.
    for key in ("valid_index", "index_token_agrees", "index_outcome_agrees"):
        if key not in incoming:
            continue
        new_value = incoming[key]
        old_value = existing.get(key)
        if old_value is None and new_value is not None:
            merged[key] = new_value
    # ``outcome_index_supplied`` is informative only: prefer True (we observed
    # one); False is recorded when explicitly False; never overwritten
    # forward from True to False because the caller may simply have lacked
    # context.
    if incoming.get("outcome_index_supplied") is True:
        merged.setdefault("outcome_index_supplied", True)
    if merged == existing:
        return existing
    return merged


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

    # Preserve ``provenance.trade_validation`` from the existing record when
    # the incoming build carries a different (or absent) trade context —
    # union of diagnostics, no churn. Substantive equality is gated ONLY on
    # the authoritative Gamma-shape namespaces below.
    existing_provenance = existing_snapshot.get("provenance")
    incoming_provenance = incoming.get("provenance")
    existing_tv = (
        existing_provenance.get("trade_validation")
        if isinstance(existing_provenance, dict)
        else None
    )
    incoming_tv = (
        incoming_provenance.get("trade_validation")
        if isinstance(incoming_provenance, dict)
        else None
    )
    if isinstance(incoming_tv, dict) and isinstance(existing_tv, dict):
        merged_tv = _merge_trade_validation(existing_tv, incoming_tv)
        if merged_tv is not existing_tv:
            output_prov = output.setdefault("provenance", {})
            if isinstance(output_prov, dict):
                output_prov["trade_validation"] = merged_tv
                changed = True
    elif isinstance(incoming_tv, dict):
        output_prov = output.setdefault("provenance", {})
        if isinstance(output_prov, dict):
            output_prov["trade_validation"] = dict(incoming_tv)
            changed = True

    for namespace in ("market", "outcomes", "lifecycle", "resolution", "provenance"):
        new_namespace = incoming.get(namespace)
        if not isinstance(new_namespace, Mapping):
            continue
        # Exclude ``trade_validation`` from the substantive namespace merge:
        # its ``errors`` list and booleans are context-dependent and must
        # never produce a ``_snapshot_provenance_*_conflict`` reason. The
        # namespace-level merge above already preserved them with union
        # semantics, so we simply skip the field here.
        if namespace == "provenance":
            new_namespace = {
                key: value
                for key, value in new_namespace.items()
                if key != "trade_validation"
            }
            if not new_namespace:
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
    """Remove audit-only timestamps, caller-only context, before replay."""
    if not isinstance(snapshot, dict):
        return snapshot
    output = json.loads(json.dumps(snapshot))
    provenance = output.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("retrieved_at", None)
        provenance.pop("provider_updated_at", None)
        # ``trade_validation`` carries the caller's optional context
        # diagnostics (selected token / outcome / index). Two builds of the
        # same authoritative Gamma evidence with different caller contexts
        # are substantively equivalent — only the substantive Gamma-shape
        # fields drive replay comparison. We strip this entire nested
        # object so replay equality is gated on Gamma evidence only; the
        # ``_merge_snapshot`` layer still preserves it on every merge
        # without using it as a substantive-equality signal.
        provenance.pop("trade_validation", None)
        # Wallet-context fields (``trade_response_*``) are caller-only
        # echoes too: a Gamma fetch with vs without trade context produces
        # different ``trade_response_*`` strings even though the substantive
        # evidence is identical. The merge layer preserves them on every
        # merge via ``_merge_trade_validation`` / per-key handling, so the
        # replay layer ignores them.
        for key in list(provenance):
            if key.startswith("trade_response_"):
                provenance.pop(key)
    return output


def merge_canonical_metadata(
    existing_json: str | None,
    gamma_market: Mapping[str, Any] | None,
    *,
    condition_id: str,
    token_id: str | None = None,
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
    raw: Mapping[str, Any] | None,
    gamma_market: Mapping[str, Any] | None,
) -> dict[str, Any] | CanonicalSourceTradeMetadata:
    """Compatibility alias for callers that have not moved to the canonical name."""
    return build_canonical_metadata(raw, gamma_market)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical-metadata trust boundary
#
# Trust is represented by ``CanonicalSourceTradeMetadata`` type identity, not
# payload contents. The canonical builder is the only production constructor.
# Ordinary mappings — even schema-perfect copies — always remain untrusted and
# must pass through bounded metadata-v1 normalization.
# ─────────────────────────────────────────────────────────────────────────────


def is_canonical_source_trade_metadata(raw: Any) -> bool:
    """Return whether ``raw`` has trusted canonical type identity.

    Ordinary mappings always return ``False`` regardless of their contents.
    This helper exists for compatibility and must never inspect mapping fields.
    """
    return type(raw) is CanonicalSourceTradeMetadata
