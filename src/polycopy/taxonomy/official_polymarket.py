"""Trusted official Polymarket taxonomy resolution; no display-text inference."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

TAXONOMY_RESOLVER_VERSION = "official-polymarket-taxonomy-v1"
TAXONOMY_USABLE = "USABLE"
TAXONOMY_PARTIAL = "PARTIAL"
TAXONOMY_UNAVAILABLE = "UNAVAILABLE"
TAXONOMY_CONFLICT = "CONFLICT"

# Explicit official broad roots only. Keys cover official root labels/slugs, not
# title-like text discovered elsewhere in an object.
OFFICIAL_BROAD_CATEGORY_MAP_V1 = {
    "politics": "politics", "sports": "sports", "esports": "esports",
    "crypto": "crypto", "culture": "culture", "mentions": "mentions",
    "weather": "weather", "economics": "economics", "tech": "tech",
    "finance": "finance",
}


@dataclass(frozen=True)
class OfficialTaxonomyResult:
    status: str
    category_label: str | None
    source: str | None
    reason_codes: tuple[str, ...]
    market_category_value: str | None
    market_tags: tuple[dict[str, str | None], ...]
    event_category_value: str | None
    event_tags: tuple[dict[str, str | None], ...]
    series_category_value: str | None
    series_tags: tuple[dict[str, str | None], ...]
    conflicts: tuple[str, ...]
    resolver_version: str = TAXONOMY_RESOLVER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, list) and value:
        return _mapping(value[0])
    return _mapping(value)


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _tags(value: Any) -> tuple[dict[str, str | None], ...]:
    if not isinstance(value, list):
        return ()
    seen: dict[tuple[str | None, str | None, str | None], dict[str, str | None]] = {}
    for item in value:
        raw = _mapping(item)
        # A scalar tag has no trusted label/slug provenance and cannot map.
        if not raw:
            continue
        tag = {"id": _clean(raw.get("id")), "label": _clean(raw.get("label") or raw.get("name")), "slug": _clean(raw.get("slug"))}
        seen.setdefault((tag["id"], tag["label"], tag["slug"]), tag)
    return tuple(sorted(seen.values(), key=lambda x: ((x["slug"] or "").lower(), (x["label"] or "").lower(), x["id"] or "")))


def _map_category(value: str | None) -> str | None:
    return OFFICIAL_BROAD_CATEGORY_MAP_V1.get(value.lower()) if value else None


def _tag_categories(tags: Iterable[dict[str, str | None]]) -> set[str]:
    found: set[str] = set()
    for tag in tags:
        # Exact official roots only; a tag's arbitrary title does not qualify.
        for value in (tag.get("slug"), tag.get("label")):
            mapped = _map_category(value)
            if mapped:
                found.add(mapped)
    return found


class OfficialPolymarketTaxonomyResolverV1:
    """Resolve source precedence, retaining conflict provenance fail-closed."""

    def resolve(self, market: Mapping[str, Any] | None) -> OfficialTaxonomyResult:
        raw_market = _mapping(market)
        event = _first_mapping(raw_market.get("events") or raw_market.get("event"))
        series = _first_mapping(raw_market.get("series"))
        values = {
            "market": _clean(raw_market.get("category")),
            "event": _clean(event.get("category")),
            "series": _clean(series.get("category")),
        }
        tags = {
            "market": _tags(raw_market.get("tags")),
            "event": _tags(event.get("tags")),
            "series": _tags(series.get("tags")),
        }
        candidates: list[tuple[str, str]] = []
        if (cat := _map_category(values["market"])):
            candidates.append((cat, "market.category"))
        for cat in sorted(_tag_categories(tags["market"])):
            candidates.append((cat, "market.root_tag"))
        if (cat := _map_category(values["event"])):
            candidates.append((cat, "event.category"))
        for cat in sorted(_tag_categories(tags["event"])):
            candidates.append((cat, "event.root_tag"))
        if (cat := _map_category(values["series"])):
            candidates.append((cat, "series.category"))
        for cat in sorted(_tag_categories(tags["series"])):
            candidates.append((cat, "series.root_tag"))
        categories = {category for category, _ in candidates}
        base = dict(
            market_category_value=values["market"], market_tags=tags["market"],
            event_category_value=values["event"], event_tags=tags["event"],
            series_category_value=values["series"], series_tags=tags["series"],
        )
        if len(categories) > 1:
            return OfficialTaxonomyResult(TAXONOMY_CONFLICT, None, None, ("OFFICIAL_TAXONOMY_CONFLICT",), conflicts=tuple(sorted(categories)), **base)
        if len(categories) == 1:
            category = next(iter(categories))
            source = next(source for item, source in candidates if item == category)
            return OfficialTaxonomyResult(TAXONOMY_USABLE, category, source, (), conflicts=(), **base)
        any_official = any((values["market"], values["event"], values["series"], tags["market"], tags["event"], tags["series"]))
        return OfficialTaxonomyResult(TAXONOMY_PARTIAL if any_official else TAXONOMY_UNAVAILABLE, None, None, ("OFFICIAL_TAGS_UNMAPPED" if any_official else "OFFICIAL_TAXONOMY_UNAVAILABLE",), conflicts=(), **base)


__all__ = ["OFFICIAL_BROAD_CATEGORY_MAP_V1", "OfficialPolymarketTaxonomyResolverV1", "OfficialTaxonomyResult", "TAXONOMY_CONFLICT", "TAXONOMY_PARTIAL", "TAXONOMY_UNAVAILABLE", "TAXONOMY_USABLE", "TAXONOMY_RESOLVER_VERSION"]
