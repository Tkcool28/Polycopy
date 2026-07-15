from __future__ import annotations

from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_CONFLICT,
    TAXONOMY_PARTIAL,
    TAXONOMY_UNAVAILABLE,
    TAXONOMY_USABLE,
    OfficialPolymarketTaxonomyResolverV1,
)


def test_explicit_market_category_wins() -> None:
    result = OfficialPolymarketTaxonomyResolverV1().resolve({"category": "Sports"})
    assert result.status == TAXONOMY_USABLE
    assert result.category_label == "sports"
    assert result.source == "market.category"


def test_exact_official_root_market_tag_is_usable() -> None:
    result = OfficialPolymarketTaxonomyResolverV1().resolve({"tags": [{"id": "1", "label": "Crypto", "slug": "crypto"}]})
    assert result.status == TAXONOMY_USABLE
    assert result.category_label == "crypto"
    assert result.source == "market.root_tag"


def test_event_fallback_and_series_fallback() -> None:
    resolver = OfficialPolymarketTaxonomyResolverV1()
    event = resolver.resolve({"events": [{"tags": [{"slug": "weather", "label": "Weather"}]}]})
    series = resolver.resolve({"series": [{"category": "Finance"}]})
    assert (event.status, event.category_label, event.source) == (TAXONOMY_USABLE, "weather", "event.root_tag")
    assert (series.status, series.category_label, series.source) == (TAXONOMY_USABLE, "finance", "series.category")


def test_specific_tag_does_not_become_category() -> None:
    result = OfficialPolymarketTaxonomyResolverV1().resolve({"tags": [{"slug": "donald-trump", "label": "Donald Trump"}]})
    assert result.status == TAXONOMY_PARTIAL
    assert result.category_label is None


def test_conflicting_trusted_categories_fail_closed() -> None:
    result = OfficialPolymarketTaxonomyResolverV1().resolve({"category": "Sports", "tags": [{"slug": "crypto", "label": "Crypto"}]})
    assert result.status == TAXONOMY_CONFLICT
    assert result.category_label is None


def test_display_text_never_used_and_no_evidence_is_unavailable() -> None:
    result = OfficialPolymarketTaxonomyResolverV1().resolve({"groupItemTitle": "Politics", "title": "Politics", "question": "Politics?", "slug": "politics"})
    assert result.status == TAXONOMY_UNAVAILABLE
    assert result.category_label is None


def test_provenance_is_deterministic_and_tags_deduplicate() -> None:
    resolver = OfficialPolymarketTaxonomyResolverV1()
    payload = {"tags": [{"id": "2", "label": "Sports", "slug": "sports"}, {"id": "2", "label": "Sports", "slug": "sports"}]}
    assert resolver.resolve(payload).to_dict() == resolver.resolve(dict(payload)).to_dict()
