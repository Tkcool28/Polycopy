"""PR66 bounded source-trade-only resolution via the trusted market-state path.

This module resolves *only* rows in ``source_trades``. It never touches
markets, market_outcomes, copy candidates, snapshots, decisions, scores,
signals, orders, or positions.

Trusted truth
=============

Truth is obtained through the **proven** ``PolymarketPublicAdapter.get_market``
market-state evidence path — the *same* path PR24V/PR24W reuse via
``LiveGammaMarketStateProvider``. ``get_market`` is the only Gamma lookup that
correctly routes **condition IDs** to ``GET /markets?condition_ids=<hex>``;
the legacy ``ResolutionProvider.check_resolution`` mistakenly hits
``GET /markets/{id}`` which treats ``{id}`` as a *numeric* Gamma market id and
returns HTTP 422 for a hex condition id. We therefore deliberately do NOT use
``check_resolution`` here.

Persisted ``markets.resolved`` / ``markets.winning_token_id`` /
``market_outcomes`` values are NEVER the authority. The winner is always
re-derived through ``derive_winner_from_market_payload`` from the provider's
live market object.

Canonical routing contract
==========================

``get_market`` performs explicit, shape-based routing (NOT heuristic guessing):

* hex ``0x`` + 64 hex chars  -> condition-ID query-param lookup
  ``GET /markets?condition_ids=<hex>`` (list; exact-identity select)
* all-digits                -> numeric Gamma market-ID path lookup
  ``GET /markets/{id}``
* anything else             -> rejected (``missing_market_identity`` for our
  purposes); we never call an incompatible endpoint with it.

Token IDs cannot be routed by Gamma ``get_market`` (which keys on
conditionId); a row carrying only a token id is reported honestly as
``missing_market_identity`` (token->condition mapping is a deferred helper,
mirroring PR24V's ``unresolvable_token_id_only``). Provider results are
de-duplicated by canonical condition id within one run (one network call per
unique market).

Error classification
====================

A provider failure is classified into exactly one bucket — an HTTP routing
error is NOT "malformed market truth":

* ``routing_http_error``     — endpoint reachable but returned non-2xx for the
  *route* we chose (e.g. 404/422/400). Carries route type, id prefix, status,
  concise reason. No truth is guessed.
* ``provider_unavailable``   — network/transport/5xx/timeout: the provider
  could not be reached (bounded, no guess).
* ``malformed_payload``      — a 2xx response we could not parse into a valid
  Market / truth (bad JSON, unparseable, ambiguous selection).
* ``unavailable``            — provider returned None (unknown / not-found).

BUY vs SELL
===========

* BUY — when truth is a complete single winner, we call the frozen helper
  ``settle_source_trade_against_truth`` (unchanged). Eligible persisted
  fields: ``resolution_status``, ``resolved_at``, ``winning_token_id``,
  ``is_winning_trade``, ``realized_pnl``, ``settlement_source``.
* SELL — remains documentation-only evidence. We do NOT call BUY settlement
  accounting, do NOT assign ``is_winning_trade``, do NOT compute
  ``realized_pnl``, and do NOT write any won/lost label. SELL rows are
  counted as ``unsupported_sell_accounting`` and left exactly as-is.

resolved_at semantics
=====================

We prefer the provider's trusted resolution timing when available. The
provider returns a Gamma market object; ``Market.fetched_at`` is the moment
we observed it, NOT the market's true resolution time. We therefore record
``resolved_at`` only when a trustworthy timestamp is available; otherwise we
leave it ``None`` and the report includes ``missing_resolution_timestamp`` so
the omission is explicit. We never fabricate the market's true resolution
time. ``resolved_at`` is an observation time and is excluded from the
idempotency/conflict comparison (only resolution *facts* are compared).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.domain.market import Market
from polycopy.engine.market_resolution_truth import (
    AmbiguousResolution,
    MarketResolutionTruth,
    derive_winner_from_market_payload,
)
from polycopy.engine.trade_settlement import settle_source_trade_against_truth
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


@dataclass
class ResolveReport:
    """Structured, bounded report for one bounded resolver run."""

    wallet_prefix: Optional[str] = None
    examined: int = 0
    buy_examined: int = 0
    sell_examined: int = 0
    unique_markets_checked: int = 0
    provider_calls: int = 0
    resolvable: int = 0
    unresolved: int = 0
    unavailable: int = 0
    routing_http_error: int = 0
    provider_unavailable: int = 0
    malformed_payload: int = 0
    ambiguous: int = 0
    missing_market_identity: int = 0
    missing_winning_token: int = 0
    unsupported_sell_accounting: int = 0
    already_resolved: int = 0
    identical_noop: int = 0
    conflicts: int = 0
    would_update: int = 0
    updated: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    missing_resolution_timestamp: int = 0
    dry_run: bool = True
    live_read_performed: bool = False
    committed: bool = False
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet_prefix": self.wallet_prefix,
            "examined": self.examined,
            "buy_examined": self.buy_examined,
            "sell_examined": self.sell_examined,
            "unique_markets_checked": self.unique_markets_checked,
            "provider_calls": self.provider_calls,
            "resolvable": self.resolvable,
            "unresolved": self.unresolved,
            "unavailable": self.unavailable,
            "routing_http_error": self.routing_http_error,
            "provider_unavailable": self.provider_unavailable,
            "malformed_payload": self.malformed_payload,
            "ambiguous": self.ambiguous,
            "missing_market_identity": self.missing_market_identity,
            "missing_winning_token": self.missing_winning_token,
            "unsupported_sell_accounting": self.unsupported_sell_accounting,
            "already_resolved": self.already_resolved,
            "identical_noop": self.identical_noop,
            "conflicts": self.conflicts,
            "would_update": self.would_update,
            "updated": self.updated,
            "errors": self.errors,
            "missing_resolution_timestamp": self.missing_resolution_timestamp,
            "dry_run": self.dry_run,
            "live_read_performed": self.live_read_performed,
            "committed": self.committed,
            "duration_seconds": round(self.duration_seconds, 4),
        }


# ---------------------------------------------------------------------------
# Provider result classification
# ---------------------------------------------------------------------------

_RESOLVED = "resolved"
_UNRESOLVED = "unresolved"
_UNAVAILABLE = "unavailable"
_ROUTING_HTTP_ERROR = "routing_http_error"
_PROVIDER_UNAVAILABLE = "provider_unavailable"
_MALFORMED_PAYLOAD = "malformed_payload"
_AMBIGUOUS = "ambiguous"
_MISSING_WINNING_TOKEN = "missing_winning_token"


class _ProviderOutcome:
    """Normalized outcome of a single market truth lookup."""

    def __init__(
        self,
        state: str,
        truth: Optional[MarketResolutionTruth] = None,
        error: Optional[str] = None,
        *,
        route_type: Optional[str] = None,
        http_status: Optional[int] = None,
    ) -> None:
        self.state = state
        self.truth = truth
        self.error = error
        self.route_type = route_type
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Market-state provider seam (reuses PolymarketPublicAdapter.get_market)
# ---------------------------------------------------------------------------


class MarketStateProvider(Protocol):
    """Duck-typed provider exposing the proven ``get_market`` evidence path.

    ``PolymarketPublicAdapter`` satisfies this directly. Tests inject a fake.
    """

    async def get_market(self, market_id: str) -> Optional[Market]:
        """Fetch one market by condition id or numeric Gamma market id."""
        ...


# Route-shape helpers (mirrors PolymarketPublicAdapter._is_* for independence).
_CONDITION_ID_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_NUMERIC_ID_RE = re.compile(r"[0-9]+")


def classify_market_identity(row: Mapping[str, Any]) -> Optional[str]:
    """Return the routable canonical market identity for a source-trade row.

    Only ``market_source_id`` (Polymarket condition id or numeric Gamma id) is
    routable. A bare token id (``token_id``) cannot be routed to Gamma
    ``get_market`` (which keys on conditionId) — it is reported as
    ``missing_market_identity`` and never guessed.

    Accepts dict, sqlite3.Row, or any mapping-like object.
    """
    try:
        raw = row["market_source_id"]
    except (KeyError, IndexError, TypeError):
        try:
            raw = row.get("market_source_id")  # type: ignore[attr-defined]
        except AttributeError:
            raw = None
    if raw is None:
        return None
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return str(raw).strip() or None


def _route_type_for(identifier: str) -> str:
    """Explicit, shape-based route selection (no heuristic guessing)."""
    if _CONDITION_ID_RE.fullmatch(identifier):
        return "gamma_condition_id"
    if _NUMERIC_ID_RE.fullmatch(identifier):
        return "gamma_numeric_id"
    return "unsupported"


def _identifier_prefix(value: str, n: int = 12) -> str:
    return str(value)[:n]


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------


async def _check_one_market(
    provider: MarketStateProvider,
    identifier: str,
    *,
    report: ResolveReport,
) -> _ProviderOutcome:
    """Query the trusted market-state provider for one identifier and classify.

    Uses ``provider.get_market`` (the proven PR24V path), which routes hex
    condition ids to ``GET /markets?condition_ids=<hex>`` and numeric ids to
    ``GET /markets/{id}``. Exactly one call per unique identifier per run; the
    caller de-duplicates.
    """
    report.provider_calls += 1
    route_type = _route_type_for(identifier)
    try:
        market = await provider.get_market(identifier)
    except Exception as exc:  # noqa: BLE001 - bounded, reported, never guessed
        # Distinguish an HTTP routing error (endpoint returned non-2xx) from a
        # transport/5xx/timeout (provider unreachable). httpx exposes status_code
        # on httpx.HTTPStatusError; other exceptions are "provider_unavailable".
        status = getattr(exc, "response", None)
        status = getattr(status, "status_code", None) if status is not None else None
        if status is not None:
            return _ProviderOutcome(
                _ROUTING_HTTP_ERROR,
                error=f"http_{status}:{type(exc).__name__}",
                route_type=route_type,
                http_status=status,
            )
        return _ProviderOutcome(
            _PROVIDER_UNAVAILABLE,
            error=f"provider_error:{type(exc).__name__}",
            route_type=route_type,
        )

    # Provider explicitly returned None (404 / unknown condition id).
    if market is None:
        return _ProviderOutcome(_UNAVAILABLE, route_type=route_type)

    # Derive truth from the live market object (never from stale persisted rows).
    try:
        checked_at = _now_utc_z()
        market_payload: Any = (
            market.model_dump() if hasattr(market, "model_dump") else market
        )
        truth = derive_winner_from_market_payload(
            market_id=identifier,
            market=market_payload,
            source="polymarket_gamma",
            checked_at=checked_at,
        )
    except AmbiguousResolution:
        return _ProviderOutcome(_AMBIGUOUS, route_type=route_type)
    except Exception as exc:  # noqa: BLE001 - malformed payload
        return _ProviderOutcome(
            _MALFORMED_PAYLOAD,
            error=f"derive_error:{type(exc).__name__}",
            route_type=route_type,
        )

    # Distinguish the two "no winner token" cases that derive_winner collapses
    # into resolved=False:
    #  * market genuinely unresolved / not final  -> unresolved
    #  * market resolved (provider confirmed final) but no winner token
    #    derivable from outcomes                  -> missing_winning_token
    market_resolved = _market_resolved_flag(market)
    if market_resolved and (not truth.resolved or truth.winning_token_id is None):
        return _ProviderOutcome(
            _MISSING_WINNING_TOKEN, truth=truth, route_type=route_type
        )
    if not truth.resolved or truth.winning_token_id is None:
        return _ProviderOutcome(_UNRESOLVED, route_type=route_type)

    return _ProviderOutcome(_RESOLVED, truth=truth, route_type=route_type)


async def _resolve_rows(
    conn: Any,
    *,
    provider: Optional[MarketStateProvider],
    wallet: Optional[str],
    limit: int,
    unresolved_only: bool,
    apply: bool,
    report: ResolveReport,
) -> None:
    """Internal async resolver over an already-open sqlite3 connection."""
    sql = "SELECT st.* FROM source_trades st WHERE 1=1"
    params: list[Any] = []
    if wallet:
        sql += " AND lower(st.trader_address)=?"
        params.append(wallet.lower())
    if unresolved_only:
        sql += " AND COALESCE(st.resolution_status, 'unresolved')='unresolved'"
    sql += " ORDER BY st.timestamp, st.id LIMIT ?"
    params.append(max(1, min(int(limit), 500)))

    rows = conn.execute(sql, params).fetchall()

    # De-duplicate provider calls by canonical identifier within this run.
    truth_cache: dict[str, _ProviderOutcome] = {}
    updates: list[tuple[str, dict[str, Any]]] = []

    for row in rows:
        report.examined += 1
        side = str(row["side"] or "").upper()
        if side == "BUY":
            report.buy_examined += 1
        elif side == "SELL":
            report.sell_examined += 1

        identifier = classify_market_identity(row)
        if not identifier:
            report.missing_market_identity += 1
            report.errors.append(
                {
                    "source_trade_id": _short(row["source_trade_id"]),
                    "market_identifier": "",
                    "error_type": "missing_market_identity",
                    "message": "row has no routable market_source_id (token_id alone is not routable)",
                }
            )
            continue

        # SELL: documentation-only. Never perform BUY settlement accounting.
        if side == "SELL":
            report.unsupported_sell_accounting += 1
            continue

        # BUY: need trusted truth via the provider.
        if provider is None:
            # No --allow-live: cannot refresh truth; treat as unavailable.
            report.unavailable += 1
            continue

        if identifier not in truth_cache:
            outcome = await _check_one_market(provider, identifier, report=report)
            truth_cache[identifier] = outcome
            report.unique_markets_checked += 1
        else:
            outcome = truth_cache[identifier]

        route_type = outcome.route_type or _route_type_for(identifier)
        id_prefix = _identifier_prefix(identifier)
        st = _short(row["source_trade_id"])

        if outcome.state == _RESOLVED:
            truth = outcome.truth
            assert truth is not None
            settlement = settle_source_trade_against_truth(
                source_trade=row,
                market_truth=truth,
                settlement_source="pr66_source_trade_resolution",
                resolved_at=truth.checked_at,
            )
            report.resolvable += 1

            current_status = row["resolution_status"] or "unresolved"
            current_win = row["winning_token_id"]
            current_flag = row["is_winning_trade"]
            current_pnl = row["realized_pnl"]
            current_source = row["settlement_source"]

            if current_status != "unresolved":
                # Already resolved — compare for conflict / identical.
                # ``resolved_at`` (provider observation time) is excluded from
                # the identity/conflict comparison; only resolution *facts* are.
                same = (
                    current_status == settlement.resolution_status
                    and current_win == settlement.winning_token_id
                    and current_flag == settlement.is_winning_trade
                    and _pnl_equal(current_pnl, settlement.realized_pnl)
                    and current_source == settlement.settlement_source
                )
                if same:
                    report.identical_noop += 1
                else:
                    report.conflicts += 1
                    report.errors.append(
                        {
                            "source_trade_id": st,
                            "market_identifier": _short(identifier),
                            "error_type": "conflict",
                            "message": _conflict_message(row, settlement, truth),
                        }
                    )
                report.already_resolved += 1
                continue

            # Unresolved + complete truth -> eligible to update.
            if settlement.resolved_at is None:
                report.missing_resolution_timestamp += 1
            report.would_update += 1
            if apply:
                updates.append(
                    (
                        row["id"],
                        {
                            "resolution_status": settlement.resolution_status,
                            "resolved_at": settlement.resolved_at,
                            "winning_token_id": settlement.winning_token_id,
                            "is_winning_trade": settlement.is_winning_trade,
                            "realized_pnl": settlement.realized_pnl,
                            "settlement_source": settlement.settlement_source,
                        },
                    )
                )
        elif outcome.state == _UNRESOLVED:
            report.unresolved += 1
        elif outcome.state == _UNAVAILABLE:
            report.unavailable += 1
        elif outcome.state == _ROUTING_HTTP_ERROR:
            report.routing_http_error += 1
            report.errors.append(
                {
                    "source_trade_id": st,
                    "market_identifier": _short(identifier),
                    "error_type": "routing_http_error",
                    "message": outcome.error or "provider returned HTTP non-2xx for the chosen route",
                    "route_type": route_type,
                    "identifier_prefix": id_prefix,
                    "http_status": str(outcome.http_status or ""),
                }
            )
        elif outcome.state == _PROVIDER_UNAVAILABLE:
            report.provider_unavailable += 1
            report.errors.append(
                {
                    "source_trade_id": st,
                    "market_identifier": _short(identifier),
                    "error_type": "provider_unavailable",
                    "message": outcome.error or "provider unreachable (transport/5xx/timeout)",
                    "route_type": route_type,
                    "identifier_prefix": id_prefix,
                }
            )
        elif outcome.state == _MALFORMED_PAYLOAD:
            report.malformed_payload += 1
            report.errors.append(
                {
                    "source_trade_id": st,
                    "market_identifier": _short(identifier),
                    "error_type": "malformed_payload",
                    "message": outcome.error or "provider returned an unparseable payload",
                    "route_type": route_type,
                    "identifier_prefix": id_prefix,
                }
            )
        elif outcome.state == _AMBIGUOUS:
            report.ambiguous += 1
            report.errors.append(
                {
                    "source_trade_id": st,
                    "market_identifier": _short(identifier),
                    "error_type": "ambiguous",
                    "message": "provider truth ambiguous: multiple winning tokens",
                }
            )
        elif outcome.state == _MISSING_WINNING_TOKEN:
            report.missing_winning_token += 1
            report.errors.append(
                {
                    "source_trade_id": st,
                    "market_identifier": _short(identifier),
                    "error_type": "missing_winning_token",
                    "message": "market resolved but no winner token derivable",
                }
            )

    if apply and updates:
        # Per-row conflict isolation: each eligible row gets its own UPDATE
        # guarded by the still-unresolved precondition. A concurrent /
        # repeated write that already flipped status is simply skipped
        # (rowcount 0) rather than aborting the whole batch.
        for row_id, vals in updates:
            cur = conn.execute(
                "UPDATE source_trades SET "
                "resolution_status=?, resolved_at=?, winning_token_id=?, "
                "is_winning_trade=?, realized_pnl=?, settlement_source=? "
                "WHERE id=? AND COALESCE(resolution_status, 'unresolved')='unresolved'",
                (
                    vals["resolution_status"],
                    vals["resolved_at"],
                    vals["winning_token_id"],
                    vals["is_winning_trade"],
                    vals["realized_pnl"],
                    vals["settlement_source"],
                    row_id,
                ),
            )
            report.updated += max(0, cur.rowcount)


def resolve_source_trades(
    conn: Any,
    *,
    provider: Optional[MarketStateProvider] = None,
    wallet: Optional[str] = None,
    limit: int = 50,
    unresolved_only: bool = True,
    apply: bool = False,
    wallet_prefix: Optional[str] = None,
) -> ResolveReport:
    """Resolve bounded source trades via the trusted market-state path.

    ``conn`` is an already-open ``sqlite3.Connection`` (read-only URI for
    dry-run, writable for apply). This module never opens the project
    ``Database`` class, so no migration can be triggered.

    ``provider`` should expose ``get_market`` (the proven PR24V market-state
    path). When ``None`` (no ``--allow-live``), BUY rows that need fresh truth
    are reported as ``unavailable`` and SELL rows as ``unsupported_sell_accounting``
    — no network call is made and no row is mutated.
    """
    start = datetime.now(timezone.utc)
    report = ResolveReport(
        wallet_prefix=wallet_prefix,
        dry_run=not apply,
        live_read_performed=provider is not None,
    )
    asyncio.run(
        _resolve_rows(
            conn,
            provider=provider,
            wallet=wallet,
            limit=limit,
            unresolved_only=unresolved_only,
            apply=apply,
            report=report,
        )
    )
    report.duration_seconds = (datetime.now(timezone.utc) - start).total_seconds()
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_z() -> str:
    """ISO-8601 UTC timestamp terminated with 'Z' (schema convention)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _short(value: Any, n: int = 12) -> str:
    if value is None:
        return ""
    s = str(value)
    return s[:n]


def _pnl_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


def _market_resolved_flag(market: Any) -> bool:
    """Best-effort read of a market's resolved flag across pydantic/dict."""
    if hasattr(market, "resolved"):
        try:
            return bool(market.resolved)
        except Exception:
            pass
    if isinstance(market, Mapping):
        return bool(market.get("resolved", False))
    return False


def _conflict_message(row: Any, settlement: Any, truth: MarketResolutionTruth) -> str:
    diffs: list[str] = []
    if (row["resolution_status"] or "unresolved") != settlement.resolution_status:
        diffs.append(
            f"resolution_status {row['resolution_status']}->{settlement.resolution_status}"
        )
    if row["winning_token_id"] != settlement.winning_token_id:
        diffs.append("winning_token_id differs")
    if row["is_winning_trade"] != settlement.is_winning_trade:
        diffs.append("is_winning_trade differs")
    if not _pnl_equal(row["realized_pnl"], settlement.realized_pnl):
        diffs.append("realized_pnl differs")
    # ``resolved_at`` (provider observation time) is intentionally excluded:
    # it is not a resolution fact and legitimately changes each run.
    if (row["settlement_source"] or "") != settlement.settlement_source:
        diffs.append("settlement_source differs")
    return "conflicting existing resolution: " + "; ".join(diffs or ["unknown"])


def build_market_state_provider() -> MarketStateProvider:
    """Construct the trusted Polymarket public Gamma market-state provider."""
    return PolymarketPublicAdapter(
        gamma_base_url="https://gamma-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
    )


# ---------------------------------------------------------------------------
# Market-centric (specialist-evidence) extension
# ---------------------------------------------------------------------------
#
# The ``refresh_specialist_market_truth`` CLI reuses the proven
# ``get_market`` / ``derive_winner_from_market_payload`` /
# ``settle_source_trade_against_truth`` machinery above, but drives it from a
# *selected batch of markets* (selected by an exact ``--market-source-id`` /
# ``--wallet-id`` / ``--watch-id`` selector) rather than a flat wallet scan.
#
# These helpers are the ONLY addition this module needs for S4: they select
# the eligible BUY rows and run the per-market atomic settle loop. They lean
# entirely on the existing ``_check_one_market`` / ``_ProviderOutcome`` /
# ``settle_source_trade_against_truth`` / ``_pnl_equal`` helpers — no second
# parser, no second settlement calculator, no second winner-derivation path.


# Sources accepted by the specialist-evidence refresh (S3/S4 exact contract).
# ``source="polymarket"`` (a legacy catch-all) is deliberately NOT accepted.
SPECIALIST_REFRESH_SOURCES: frozenset[str] = frozenset({SOURCE_NAME, "polymarket_clob"})


def _specialist_eligible_clause() -> str:
    """Exact accepted-source predicate for the specialist refresh scan.

    Mirrors S3 exactly: an exact ``source`` value IN the accepted set, a BUY
    side, non-sample, and a non-empty ``market_source_id``.
    """
    placeholders = ", ".join("?" for _ in SPECIALIST_REFRESH_SOURCES)
    return (
        f"st.source IN ({placeholders}) "
        "AND st.side = 'BUY' "
        "AND st.is_sample = 0 "
        "AND st.market_source_id IS NOT NULL "
        "AND TRIM(st.market_source_id) != ''"
    )


def _resolve_watch_address(db: Any, watch_id: str) -> Optional[str]:
    """Resolve a ``specialist_evidence_watchlist.id`` to its canonical wallet address.

    Returns the ``wallets.address`` joined through ``wallet_id``. Returns
    ``None`` when the watch is unknown, or is paused / retired / sample. This is
    the exact S4 contract: a paused/retired/sample/unknown watch is refused.

    ``db`` is an already-open connection exposing ``execute`` / ``fetchone``.
    """
    row = db.execute(
        "SELECT w.address, w.is_sample, wl.status "
        "FROM specialist_evidence_watchlist wl "
        "JOIN wallets w ON w.id = wl.wallet_id "
        "WHERE wl.id = ?",
        (watch_id,),
    ).fetchone()
    if row is None:
        return None
    if bool(row["is_sample"]):
        return None
    if str(row["status"] or "") != "active":
        return None
    return str(row["address"])


def select_markets_for_refresh(
    db: Any,
    *,
    market_source_id: Optional[str] = None,
    wallet_address: Optional[str] = None,
    watch_id: Optional[str] = None,
    limit_markets: int = 100,
) -> list[str]:
    """Return the distinct eligible BUY market_source_ids for one selector.

    Exactly one selector must be supplied by the caller (the CLI enforces the
    "exactly one" rule before calling this). Selection:

    * ``market_source_id`` — exact ``source_trades.market_source_id`` match
      (no prefix / fuzzy matching). May point at an already-resolved market
      for conflict/idempotency proof.
    * ``wallet_address`` — resolved from a wallet UUID by the caller; selects
      markets with eligible BUY rows for that address.
    * ``watch_id`` — resolved to a wallet address via
      :func:`_resolve_watch_address`; refused when unknown/sample/paused/
      retired.

    Eligible rows are BUY, non-sample, with a non-empty ``market_source_id``,
    and an exact accepted ``source`` value. Distinct markets are bounded by
    ``limit_markets`` and ordered deterministically by ``market_source_id``.
    """
    sources = tuple(SPECIALIST_REFRESH_SOURCES)
    clauses = [_specialist_eligible_clause()]
    params: list[Any] = list(sources)
    if market_source_id:
        clauses.append("LOWER(st.market_source_id) = ?")
        params.append(market_source_id.lower())
    if wallet_address:
        clauses.append("LOWER(st.trader_address) = ?")
        params.append(wallet_address.lower())
    if watch_id:
        addr = _resolve_watch_address(db, watch_id)
        if addr is None:
            return []
        clauses.append("LOWER(st.trader_address) = ?")
        params.append(addr.lower())
    sql = (
        "SELECT DISTINCT st.market_source_id FROM source_trades st WHERE "
        + " AND ".join(clauses)
        + " ORDER BY st.market_source_id LIMIT ?"
    )
    params.append(int(limit_markets))
    rows = db.execute(sql, params).fetchall()
    return [r["market_source_id"] for r in rows]


def get_source_trades_for_market(
    db: Any, market_source_id: str
) -> list[Any]:
    """Return ALL eligible linked BUY rows for one selected market.

    Includes already-resolved rows so replay / conflict detection remain honest.
    Deterministic ordering by ``id``.
    """
    sources = tuple(SPECIALIST_REFRESH_SOURCES)
    rows = db.execute(
        "SELECT st.* FROM source_trades st WHERE "
        "LOWER(st.market_source_id) = ? AND "
        + _specialist_eligible_clause().replace("st.", "")
        + " ORDER BY st.id",
        (market_source_id.lower(), *sources),
    ).fetchall()
    return list(rows)


class MarketRefreshOutcome:
    """Per-market bookkeeping + settle result for the S4 refresh pass."""

    def __init__(self, market_source_id: str) -> None:
        self.market_source_id = market_source_id
        self.last_status: Optional[str] = None
        self.last_error: Optional[str] = None
        self.resolved_at: Optional[str] = None
        self.attempt_count: int = 1
        self.updated: int = 0
        self.conflict: bool = False
        self.noop: bool = False

    def as_bookkeeping(self) -> dict[str, Any]:
        return {
            "market_source_id": self.market_source_id,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "resolved_at": self.resolved_at,
            "attempt_count": self.attempt_count,
        }


async def resolve_selected_markets(
    db: Any,
    *,
    markets: list[str],
    provider: Optional[MarketStateProvider],
    apply: bool,
    report: ResolveReport,
    settlement_source: str = "source_trade_resolution",
) -> list[MarketRefreshOutcome]:
    """Settle one selected batch of distinct markets using the proven path.

    For each market: call the proven ``get_market`` once, derive a single
    winner truth, then compute each linked BUY row's settlement through
    ``settle_source_trade_against_truth``. Atomicity (whole-market SAVEPOINT,
    conflict isolation) is the caller's responsibility for DB writes — but the
    conflict decision itself is computed here and returned so the caller can
    either commit or roll back the whole market.

    Returns one :class:`MarketRefreshOutcome` per selected market (idempotent
    re-runs do not duplicate). ``apply`` controls whether updates are written;
    when ``False`` (dry-run) nothing is mutated and ``noop``/``conflict`` are
    still computed honestly.

    The provider must be supplied (the CLI requires ``--allow-live`` even for
    dry-run); when ``None`` every market is reported ``unavailable``.
    """
    outcomes: list[MarketRefreshOutcome] = []
    truth_cache: dict[str, _ProviderOutcome] = {}

    for cid in markets:
        mkt_out = MarketRefreshOutcome(cid)
        report.unique_markets_checked += 1
        report.provider_calls += 1

        if provider is None:
            mkt_out.last_status = _UNAVAILABLE
            mkt_out.last_error = "no_live_provider"
            report.unavailable += 1
            outcomes.append(mkt_out)
            continue

        if cid not in truth_cache:
            outcome = await _check_one_market(provider, cid, report=report)
            truth_cache[cid] = outcome
        else:
            outcome = truth_cache[cid]

        if outcome.state == _RESOLVED:
            truth = outcome.truth
            assert truth is not None
            mkt_out.last_status = "resolved"
            mkt_out.resolved_at = truth.checked_at
            rows = get_source_trades_for_market(db, cid)
            # Build expected settlements for every linked BUY row, then compare
            # against any already-resolved row for a material conflict.
            settlements = []
            conflict = False
            for row in rows:
                report.resolvable += 1
                settlement = settle_source_trade_against_truth(
                    source_trade=row,
                    market_truth=truth,
                    settlement_source=settlement_source,
                    resolved_at=truth.checked_at,
                )
                settlements.append((row, settlement))
                current_status = row["resolution_status"] or "unresolved"
                if current_status != "unresolved":
                    same = (
                        current_status == settlement.resolution_status
                        and row["winning_token_id"] == settlement.winning_token_id
                        and row["is_winning_trade"] == settlement.is_winning_trade
                        and _pnl_equal(row["realized_pnl"], settlement.realized_pnl)
                        and (row["settlement_source"] or "")
                        == settlement.settlement_source
                    )
                    if same:
                        report.identical_noop += 1
                        mkt_out.noop = True
                    else:
                        report.conflicts += 1
                        mkt_out.conflict = True
                        conflict = True
            if conflict:
                # Retain exact existing source-trade values; no writes for this
                # market. The caller rolls back the SAVEPOINT if it attempted.
                mkt_out.last_error = "conflict"
                outcomes.append(mkt_out)
                continue
            if apply:
                for row, settlement in settlements:
                    if (row["resolution_status"] or "unresolved") == "unresolved":
                        db.execute(
                            "UPDATE source_trades SET "
                            "resolution_status=?, resolved_at=?, winning_token_id=?, "
                            "is_winning_trade=?, realized_pnl=?, settlement_source=? "
                            "WHERE id=?",
                            (
                                settlement.resolution_status,
                                settlement.resolved_at,
                                settlement.winning_token_id,
                                settlement.is_winning_trade,
                                settlement.realized_pnl,
                                settlement.settlement_source,
                                row["id"],
                            ),
                        )
                        mkt_out.updated += 1
                        report.updated += 1
            else:
                # Dry-run: count would-be updates without writing.
                for row, settlement in settlements:
                    if (row["resolution_status"] or "unresolved") == "unresolved":
                        report.would_update += 1
            outcomes.append(mkt_out)
            continue

        # Non-resolved provider states: record honestly, never claim a winner.
        mkt_out.last_status = outcome.state
        if outcome.state in (_UNRESOLVED, _UNAVAILABLE):
            if outcome.state == _UNRESOLVED:
                report.unresolved += 1
            else:
                report.unavailable += 1
        elif outcome.state == _ROUTING_HTTP_ERROR:
            report.routing_http_error += 1
            mkt_out.last_error = outcome.error
        elif outcome.state == _PROVIDER_UNAVAILABLE:
            report.provider_unavailable += 1
            mkt_out.last_error = outcome.error
        elif outcome.state == _MALFORMED_PAYLOAD:
            report.malformed_payload += 1
            mkt_out.last_error = outcome.error
        elif outcome.state == _AMBIGUOUS:
            report.ambiguous += 1
            mkt_out.last_error = "ambiguous"
        elif outcome.state == _MISSING_WINNING_TOKEN:
            report.missing_winning_token += 1
            mkt_out.last_error = "missing_winning_token"
        outcomes.append(mkt_out)

    return outcomes
