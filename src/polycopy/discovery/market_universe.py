"""Short-horizon market universe crawler for PR69 discovery.

Inputs:
  * A configured ``DiscoveryAdapter`` for live Gamma paging (optional).
  * A configured ``TaxonomyEnricher`` for fallback event/series fetches.
  * A pure ``as_of`` anchor (UTC) so audit runs are reproducible.
  * Configurable caps (``preferred_days``, ``max_capital_lock_days``,
    ``resolution_buffer_days``, ``max_markets``, ``page_size``,
    ``categories``, etc.).

Output:
  * ``(classifications, audit)`` where ``classifications`` is a deterministic
    list of :class:`MarketClassification` (one per inspected market, including
    excluded ones) and ``audit`` is an immutable :class:`MarketUniverseAudit`.

Server-side filters upstream of the Universe Crawler:
  * ``active=true``
  * ``closed=false``
  * ``end_date_min=as_of``
  * ``end_date_max=as_of + (max_capital_lock_days - resolution_buffer_days)``
        — i.e. as_of + 24 days when defaults are in force.

Client-side fail-closed validation:
  * Market-end on or after as_of.
  * Valid conditionId.
  * Order book enabled / accepting orders if the upstream exposes it.
  * Taxonomy USABLE.
  * Hard horizon passes.

Every inspected market is classified into exactly one bucket:

  * ``PREFERRED_SHORT_HORIZON`` — preferred end within ``preferred_days``.
  * ``ELIGIBLE_SHORT_HORIZON`` — preferred miss, hard cap pass.
  * ``HORIZON_TOO_LONG``        — hard cap miss.
  * ``HORIZON_UNAVAILABLE``     — missing/malformed end.
  * ``HORIZON_INVALID``         — negative, naive, malformed.
  * ``TAXONOMY_PARTIAL``        — taxonomy saw specific tags only.
  * ``TAXONOMY_UNAVAILABLE``    — no category, no broad tag.
  * ``TAXONOMY_CONFLICT``       — broad categories conflict.
  * ``NOT_TRADABLE``            — order book / accepting-orders missing.
  * ``MALFORMED``               — failed schema sanity (no conditionId,
                                   no tokenIds, ended, archived, etc.).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import DiscoveryAdapter
from polycopy.discovery.taxonomy_enricher import TaxonomyEnricher
from polycopy.policy.short_horizon import (
    HORIZON_INVALID,
    HORIZON_TOO_LONG,
    HORIZON_UNAVAILABLE,
    MAX_CAPITAL_LOCK_DAYS,
    PREFERRED_END_DAYS,
    RESOLUTION_BUFFER_DAYS,
    ShortHorizonAssessment,
    evaluate_short_horizon,
)
from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_CONFLICT,
    TAXONOMY_PARTIAL,
    TAXONOMY_UNAVAILABLE,
    TAXONOMY_USABLE,
)

logger = logging.getLogger(__name__)


# Classification buckets used by the report.
PREFERRED_SHORT_HORIZON = "PREFERRED_SHORT_HORIZON"
ELIGIBLE_SHORT_HORIZON = "ELIGIBLE_SHORT_HORIZON"
HORIZON_TOO_LONG_BUCKET = "HORIZON_TOO_LONG"
HORIZON_UNAVAILABLE_BUCKET = "HORIZON_UNAVAILABLE"
HORIZON_INVALID_BUCKET = "HORIZON_INVALID"
TAXONOMY_PARTIAL_BUCKET = "TAXONOMY_PARTIAL"
TAXONOMY_UNAVAILABLE_BUCKET = "TAXONOMY_UNAVAILABLE"
TAXONOMY_CONFLICT_BUCKET = "TAXONOMY_CONFLICT"
NOT_TRADABLE_BUCKET = "NOT_TRADABLE"
MALFORMED_BUCKET = "MALFORMED"

ELIGIBLE_BUCKETS = frozenset({PREFERRED_SHORT_HORIZON, ELIGIBLE_SHORT_HORIZON})


@dataclass(frozen=True)
class MarketClassification:
    """One deterministic classification outcome per inspected market."""

    condition_id: str
    question: str | None
    end_date_iso: str | None
    category_label: str | None
    taxonomy_source: str | None
    taxonomy_status: str | None
    horizon_status: str | None
    bucket: str
    reasons: tuple[str, ...] = ()
    excluded: bool = True
    eligible: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketUniverseAudit:
    """Audit counters for a market-universe run."""

    requested_categories: tuple[str, ...] = ()
    pages_fetched: int = 0
    api_errors: tuple[tuple[str, str, int], ...] = ()
    markets_inspected: int = 0
    bucket_counts: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    request_budget_initial: int = 0
    request_budget_used: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = {**asdict(self), "api_errors": [list(e) for e in self.api_errors]}
        return out


def _iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


def _to_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


def _question(market: Mapping[str, Any]) -> str | None:
    for key in ("question", "title"):
        value = market.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _condition_id(market: Mapping[str, Any]) -> str:
    raw = market.get("conditionId") or market.get("condition_id") or ""
    return str(raw or "").strip().lower()


def _token_ids(market: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("clobTokenIds", "clob_token_ids", "tokens"):
        value = market.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                # some Gamma pages return JSON-encoded strings
                try:
                    import json
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return tuple(str(item) for item in parsed)
                except (TypeError, ValueError):
                    continue
        if isinstance(value, list):
            return tuple(str(item) for item in value if str(item))
    return ()


def _accepting_orders(market: Mapping[str, Any]) -> bool | None:
    """Best-effort check whether the market is accepting orders.

    Polymarket's Gamma payload doesn't carry a single canonical 'closed'
    field — we already filter ``closed=false`` server-side, but the upstream
    also exposes ``archived``, ``acceptingOrders``, and ``umaResolutionStatus``.
    This helper combines them so a market with ``acceptingOrders=false`` is
    excluded, not silently scored.
    """
    accepting = market.get("acceptingOrders")
    if isinstance(accepting, bool):
        return accepting
    closed = market.get("closed")
    archived = market.get("archived")
    if closed is True or archived is True:
        return False
    return None


def _hard_horizon(
    *,
    as_of: datetime,
    end_date: datetime | None,
    preferred_days: int,
    max_capital_lock_days: int,
    resolution_buffer_days: int,
    redeemed_at: datetime | None = None,
) -> ShortHorizonAssessment:
    return evaluate_short_horizon(
        as_of,
        end_date,
        actual_redeem_timestamp=redeemed_at,
        preferred_end_days=preferred_days,
        max_capital_lock_days=max_capital_lock_days,
        resolution_buffer_days=resolution_buffer_days,
    )


def _classify(
    market: Mapping[str, Any],
    *,
    assessment: ShortHorizonAssessment | None,
    taxonomy_status: str,
    taxonomy_has_label: bool,
    taxonomy_label: str | None,
    taxonomy_source: str | None,
    malformed_reasons: tuple[str, ...],
    not_tradable: bool,
) -> tuple[str, bool, tuple[str, ...]]:
    """Return ``(bucket, eligible, reasons)``.

    Order of rejection (most specific first): malformed → not tradable →
    taxonomy → horizon.
    """
    if malformed_reasons:
        return MALFORMED_BUCKET, False, malformed_reasons
    if not_tradable:
        return NOT_TRADABLE_BUCKET, False, ("not_accepting_orders",)
    if taxonomy_status == TAXONOMY_CONFLICT:
        return TAXONOMY_CONFLICT_BUCKET, False, ("taxonomy_conflict",)
    if taxonomy_status == TAXONOMY_PARTIAL:
        return TAXONOMY_PARTIAL_BUCKET, False, ("taxonomy_partial_or_specific_tag_only",)
    if taxonomy_status == TAXONOMY_UNAVAILABLE or not taxonomy_has_label:
        return TAXONOMY_UNAVAILABLE_BUCKET, False, ("taxonomy_unavailable",)
    if assessment is None:
        return MALFORMED_BUCKET, False, ("horizon_unevaluable",)
    if assessment.status == HORIZON_INVALID:
        return HORIZON_INVALID_BUCKET, False, ("horizon_invalid",)
    if assessment.status == HORIZON_UNAVAILABLE:
        return HORIZON_UNAVAILABLE_BUCKET, False, ("horizon_unavailable",)
    if assessment.status == HORIZON_TOO_LONG:
        return HORIZON_TOO_LONG_BUCKET, False, ("horizon_too_long",)
    if assessment.eligible and assessment.preferred:
        return PREFERRED_SHORT_HORIZON, True, ("preferred_short_horizon",)
    if assessment.eligible:
        return ELIGIBLE_SHORT_HORIZON, True, ("eligible_short_horizon",)
    return MALFORMED_BUCKET, False, ("horizon_unclassifiable",)


@dataclass(frozen=True)
class MarketUniverseConfig:
    """Operator-supplied caps for one audit run."""

    as_of: datetime
    preferred_days: int = PREFERRED_END_DAYS
    max_capital_lock_days: int = MAX_CAPITAL_LOCK_DAYS
    resolution_buffer_days: int = RESOLUTION_BUFFER_DAYS
    categories: tuple[str, ...] = ()
    max_markets: int = 200
    page_size: int = 100
    max_pages: int = 10
    max_requests: int = 50
    min_volume_24h: float = 0.0
    min_liquidity: float = 0.0
    timeout_seconds: float = 12.0


def validate_config(config: MarketUniverseConfig) -> None:
    if config.preferred_days <= 0:
        raise ValueError("preferred_days must be positive")
    if config.max_capital_lock_days <= 0:
        raise ValueError("max_capital_lock_days must be positive")
    if config.resolution_buffer_days < 0:
        raise ValueError("resolution_buffer_days must be non-negative")
    if config.preferred_days > config.max_capital_lock_days:
        raise ValueError("preferred_days must not exceed max_capital_lock_days")
    if config.max_capital_lock_days > 30:
        raise ValueError("max_capital_lock_days must not exceed 30")
    if config.max_markets <= 0:
        raise ValueError("max_markets must be positive")
    if config.page_size <= 0:
        raise ValueError("page_size must be positive")
    if config.max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if config.max_requests <= 0:
        raise ValueError("max_requests must be positive")
    if config.min_volume_24h < 0:
        raise ValueError("min_volume_24h must be non-negative")
    if config.min_liquidity < 0:
        raise ValueError("min_liquidity must be non-negative")


class MarketUniverseCrawler:
    """Crawl + classify the short-horizon Polymarket universe.

    The crawler is intentionally split from the audit CLI so the engine can
    be unit-tested with prebuilt payloads and a mock adapter.
    """

    def __init__(
        self,
        adapter: DiscoveryAdapter,
        enricher: TaxonomyEnricher,
        *,
        budget: _RequestBudget,
    ) -> None:
        self._adapter = adapter
        self._enricher = enricher
        self._budget = budget

    async def run(
        self,
        config: MarketUniverseConfig,
        *,
        logger_extras: list[tuple[str, str, int]] | None = None,
    ) -> tuple[tuple[MarketClassification, ...], MarketUniverseAudit]:
        validate_config(config)
        as_of = config.as_of.astimezone(timezone.utc)
        # The server-side end_date_max is end + buffer. We want to only ever
        # look at markets whose scheduled end is within
        # (max_capital_lock_days - resolution_buffer_days) days of as_of, so
        # that — even after the worst-case 6-day resolution buffer — the
        # hard-capital-lock cap of 30 days is satisfied.
        end_max = (
            as_of + timedelta(days=config.max_capital_lock_days - config.resolution_buffer_days)
        ).date().isoformat()

        # We page all categories (filter is per category) sequentially.
        # Each call to list_active_markets consumes budget pages.
        markets: list[dict[str, Any]] = []
        api_errors: list[tuple[str, str, int]] = list(logger_extras or [])
        pages_fetched = 0
        for category in config.categories:
            if self._budget.remaining <= 0:
                api_errors.append((category, "REQUEST_BUDGET_EXHAUSTED", 0))
                break
            rows, errors = await self._adapter.list_active_markets(
                end_date_min=as_of.date().isoformat(),
                end_date_max=end_max,
                tag_slug=category.lower(),
                limit=config.max_markets,
                offset=0,
                max_pages=config.max_pages,
                page_size=config.page_size,
                budget=self._budget,
            )
            pages_fetched += 1
            markets.extend(rows)
            for err in errors:
                api_errors.append((category, err.get("error_code", "ERR"), int(err.get("http_status", 0) or 0)))

        # Apply operator-level volume/liquidity filters for the report.
        if config.min_volume_24h > 0 or config.min_liquidity > 0:
            markets = [
                m for m in markets
                if float(m.get("volume24hr") or 0) >= config.min_volume_24h
                and float(m.get("liquidity") or 0) >= config.min_liquidity
            ]
        # Truncate to max_markets, deterministic by (endDate asc, conditionId asc).
        markets.sort(key=lambda m: (_iso(m.get("endDate")) or "", _condition_id(m)))
        if len(markets) > config.max_markets:
            markets = markets[: config.max_markets]
            truncated = True
        else:
            truncated = False

        classified: list[MarketClassification] = []
        bucket_counts: dict[str, int] = {}
        for market in markets:
            cls = await self._classify_one(market, as_of=as_of, config=config)
            classified.append(cls)
            bucket_counts[cls.bucket] = bucket_counts.get(cls.bucket, 0) + 1

        audit = MarketUniverseAudit(
            requested_categories=tuple(config.categories),
            pages_fetched=pages_fetched,
            api_errors=tuple(api_errors),
            markets_inspected=len(classified),
            bucket_counts=dict(sorted(bucket_counts.items())),
            truncated=truncated,
            request_budget_initial=self._budget.initial,
            request_budget_used=self._budget.used(),
        )
        return tuple(classified), audit

    async def _classify_one(
        self,
        market: dict[str, Any],
        *,
        as_of: datetime,
        config: MarketUniverseConfig,
    ) -> MarketClassification:
        condition_id = _condition_id(market)
        end_dt = _to_utc(market.get("endDate") or market.get("end_date"))
        question = _question(market)
        malformed_reasons: list[str] = []
        eligible = False
        bucket = MALFORMED_BUCKET

        if not condition_id or not condition_id.startswith("0x"):
            malformed_reasons.append("missing_or_invalid_condition_id")
        if not _token_ids(market):
            malformed_reasons.append("missing_token_ids")

        accepting = _accepting_orders(market)
        not_tradable = accepting is False

        # Run the taxonomy enricher (no extra fetches when status was USABLE
        # in the embedded view).
        enrichment = await self._enricher.enrich_one(market)
        taxonomy_status = enrichment.result.status
        taxonomy_label = enrichment.result.category_label if taxonomy_status == TAXONOMY_USABLE else None
        taxonomy_has_label = taxonomy_label is not None
        taxonomy_source = enrichment.source_used

        assessment: ShortHorizonAssessment | None = None
        if end_dt is None:
            malformed_reasons.append("missing_end_date")
        else:
            assessment = _hard_horizon(
                as_of=as_of,
                end_date=end_dt,
                preferred_days=config.preferred_days,
                max_capital_lock_days=config.max_capital_lock_days,
                resolution_buffer_days=config.resolution_buffer_days,
            )

        # When the market is malformed in any structural way, suppress the
        # taxonomy/horizon error budget and fail closed immediately.
        if malformed_reasons:
            bucket, eligible, classification_reasons = _classify(
                market,
                assessment=assessment,
                taxonomy_status=taxonomy_status,
                taxonomy_has_label=taxonomy_has_label,
                taxonomy_label=taxonomy_label,
                taxonomy_source=taxonomy_source,
                malformed_reasons=tuple(malformed_reasons),
                not_tradable=not_tradable,
            )
            return MarketClassification(
                condition_id=condition_id,
                question=question,
                end_date_iso=_iso(market.get("endDate")),
                category_label=None,
                taxonomy_source=taxonomy_source,
                taxonomy_status=taxonomy_status,
                horizon_status=assessment.status if assessment else None,
                bucket=bucket,
                reasons=classification_reasons,
                excluded=True,
                eligible=False,
            )

        bucket, eligible, classification_reasons = _classify(
            market,
            assessment=assessment,
            taxonomy_status=taxonomy_status,
            taxonomy_has_label=taxonomy_has_label,
            taxonomy_label=taxonomy_label,
            taxonomy_source=taxonomy_source,
            malformed_reasons=(),
            not_tradable=not_tradable,
        )
        excluded = bucket not in ELIGIBLE_BUCKETS
        return MarketClassification(
            condition_id=condition_id,
            question=question,
            end_date_iso=_iso(market.get("endDate")),
            category_label=taxonomy_label,
            taxonomy_source=taxonomy_source,
            taxonomy_status=taxonomy_status,
            horizon_status=assessment.status if assessment else None,
            bucket=bucket,
            reasons=classification_reasons,
            excluded=excluded,
            eligible=eligible,
        )


__all__ = [
    "ELIGIBLE_BUCKETS",
    "ELIGIBLE_SHORT_HORIZON",
    "HORIZON_INVALID_BUCKET",
    "HORIZON_TOO_LONG_BUCKET",
    "HORIZON_UNAVAILABLE_BUCKET",
    "MALFORMED_BUCKET",
    "MarketClassification",
    "MarketUniverseAudit",
    "MarketUniverseConfig",
    "MarketUniverseCrawler",
    "NOT_TRADABLE_BUCKET",
    "PREFERRED_SHORT_HORIZON",
    "TAXONOMY_CONFLICT_BUCKET",
    "TAXONOMY_PARTIAL_BUCKET",
    "TAXONOMY_UNAVAILABLE_BUCKET",
    "validate_config",
]
