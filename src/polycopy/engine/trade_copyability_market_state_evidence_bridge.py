"""PR24V — Trade Copyability MARKET-STATE / END-TIME EVIDENCE BRIDGE.

PR24U proved the real CLOB /book collection path is wireable and REUSED the
existing read-only ``PolymarketClobClient``, but it also proved that /book does
NOT expose the market metadata required before any honest Trade Copyability
decision:

  * market_active
  * market_closed
  * market_resolved
  * market end time (end_date)
  * seconds_to_market_end
  * market identifier mapping status
  * metadata fetched_at

PR24V closes THAT gap by proving whether Polycopy can obtain the missing market
metadata for eligible ``source_trades`` rows.

This module is PURE and NON-PERSISTING (same guardrails as every Polycopy
read-only audit PR):

  * It reads ``source_trades`` through a caller-supplied ``sqlite3.Connection``
    opened with ``mode=ro``.
  * It REUSES the existing read-only ``polycopy.adapters.polymarket.
    PolymarketPublicAdapter.get_market(condition_id)`` — Gamma
    ``GET /markets/{condition_id}`` — to fetch market metadata. That adapter is
    already the project's Gamma/CLOB/data-api read-only client (no auth, no
    signing, no orders). PR24V does NOT invent a duplicate client.
  * Real metadata fetch is OPT-IN only (``--allow-live-preview``). The default
    is a dry-run that makes NO network call and reports which rows are
    *mappable* and which metadata fields *would* be available.
  * It is a DRY-RUN / report-only bridge. It never writes the production DB,
    never creates candidates / paper signals / orders / positions, never wires
    automation, never tunes any formula.

Two metadata modes:

  * ``--allow-live-preview`` OFF (DEFAULT): pure dry-run. No network call is
    made. The module proves which rows are *eligible* and *mappable* and what
    fields *would* be available, using an injectable provider
    (``MarketStateProvider``) that defaults to offline / no fetch.
  * ``--allow-live-preview`` ON: a real read-only Gamma ``get_market`` fetch is
    performed per mappable condition_id via ``PolymarketPublicAdapter`` and
    shaped into the report. STILL no persistence — the run is a live metadata
    *preview* only. Network/auth/parse failures are captured per-row and never
    crash the batch.

Eligibility reuses PR24S / PR24R / PR24U logic: canonical side == BUY,
``source_trade_id`` present, ``token_id`` OR ``market_source_id`` present (NOT
NULL), ``price`` parseable, ``size`` parseable. Mappability (resolvable to a
Gamma market) additionally requires a usable market identifier
(``market_source_id`` / ``condition_id`` / ``market_id``) — token_id alone
cannot be resolved by Gamma ``get_market`` (which keys on conditionId), so such
rows are reported honestly as ``unresolvable_token_id_only`` (a deferred
token→condition mapping helper would be needed).

All ``ready_*`` flags are ALWAYS ``False``:

  * ready_to_wire_to_automation
  * ready_to_persist_decisions
  * ready_to_create_candidates
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

# Reuse the EXISTING shared read-only helpers from the PR24U bridge (which in
# turn reuse PR24S / PR24R helpers). No duplication of field-mapping logic.
from polycopy.engine.trade_copyability_real_snapshot_collection_bridge import (
    _row_looks_sample_like,
    _safe_count,
    _table_exists,
    canonicalize_source_side,
)

# Reused readiness thresholds from Trade Copyability v1 (read-only reference;
# PR24V does not change them, only reports against them).
MIN_COPY_CANDIDATE_FILL_PERCENTAGE = 0.80  # noqa: F401  (kept for parity)

SELL_UNSUPPORTED_REASON = "sell_side_copyability_not_supported_v1"


# ── Counted tables (read-only; any that exist are reported) ────────────────
_COUNT_TABLES = (
    "source_trades",
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "wallet_score_decisions",
    "settlement_accounting_ledger",
    "orders",
    "positions",
)


# ── Injectable metadata provider interface ─────────────────────────────────
class MarketStateProvider(Protocol):
    """Narrow structural-typing protocol for a market-metadata fetcher.

    The default implementation is OFFLINE and performs no network call. Tests
    inject a subclass returning a synthetic ``Market``-like object (or a real
    one). Production live preview uses a thin async adapter around
    ``PolymarketPublicAdapter.get_market`` (``LiveGammaMarketStateProvider``)
    but ONLY when the caller explicitly opts in via ``--allow-live-preview``;
    otherwise the offline default is used and NO network call is made.

    The returned object only needs duck-typed attributes consumed by
    :func:`_normalize_market_state`: ``active``, ``closed``, ``resolved``,
    ``end_date`` (datetime or None), ``source_id``, ``fetched_at`` (datetime).
    ``PolymarketPublicAdapter``'s ``Market`` domain model already satisfies
    this contract.
    """

    async def fetch_market_state(self, *, condition_id: str) -> Any:  # pragma: no cover - Protocol
        ...


@dataclass
class _MarketStateFetchResult:
    """Normalized outcome of one metadata lookup attempt.

    ``fetched`` is True only when a real network fetch was attempted. The
    offline provider sets it False. ``market`` is the duck-typed market object
    (or None when not found / not fetched / errored).
    """

    condition_id: str
    fetched: bool = False
    market: Any = None
    error_code: Optional[str] = "OFFLINE_NO_FETCH"
    error_message: Optional[str] = "offline provider; no live fetch performed"
    fetched_at: Optional[datetime] = None


class OfflineMarketStateProvider:
    """Pure OFFLINE default provider. Performs NO network call."""

    async def fetch_market_state(self, *, condition_id: str) -> _MarketStateFetchResult:
        return _MarketStateFetchResult(
            condition_id=condition_id,
            fetched=False,
            market=None,
            error_code="OFFLINE_NO_FETCH",
            error_message="offline provider; no live Gamma fetch performed",
            fetched_at=None,
        )


class LiveGammaMarketStateProvider:
    """Async provider that REUSES ``PolymarketPublicAdapter.get_market``.

    This is the ONLY place PR24V touches a real network client, and it is
    reachable ONLY when ``--allow-live-preview`` is set. It never writes. A
    failed fetch records a bounded ``error_code`` rather than raising, so one
    bad market does not abort the whole batch.
    """

    def __init__(self, *, adapter: Any) -> None:
        # ``adapter`` is a ``PolymarketPublicAdapter`` (or any async
        # ``get_market(condition_id) -> Optional[Market]`` duck).
        self._adapter = adapter

    async def fetch_market_state(self, *, condition_id: str) -> _MarketStateFetchResult:
        if not condition_id:
            return _MarketStateFetchResult(
                condition_id=condition_id,
                fetched=False,
                market=None,
                error_code="EMPTY_CONDITION_ID",
                error_message="condition_id is empty; cannot fetch",
                fetched_at=None,
            )
        try:
            market = await self._adapter.get_market(condition_id)
        except Exception as exc:  # controlled: never crash the whole run
            return _MarketStateFetchResult(
                condition_id=condition_id,
                fetched=True,
                market=None,
                error_code="FETCH_ERROR",
                error_message=f"{type(exc).__name__}: {exc}"[:300],
                fetched_at=datetime.now(timezone.utc),
            )
        if market is None:
            return _MarketStateFetchResult(
                condition_id=condition_id,
                fetched=True,
                market=None,
                error_code="MARKET_NOT_FOUND",
                error_message="Gamma get_market returned None (404 / unknown condition_id)",
                fetched_at=datetime.now(timezone.utc),
            )
        return _MarketStateFetchResult(
            condition_id=condition_id,
            fetched=True,
            market=market,
            error_code=None,
            error_message=None,
            fetched_at=getattr(market, "fetched_at", None) or datetime.now(timezone.utc),
        )


# ── Dataclasses (report shapes) ─────────────────────────────────────────────
@dataclass(frozen=True)
class TradeCopyabilityMarketStateRowReport:
    """Per-source-trade MARKET-STATE evidence dry-run report row.

    Carries the exact fields enumerated in the PR24V task spec, plus the
    mappability / identifier-mapping status and PR24U/PR24S combination flag.
    """

    source_trade_id: Optional[str]
    wallet_address: Optional[str]
    market_source_id: Optional[str]
    token_id: Optional[str]
    side: Optional[str]
    eligibility_status: str  # "eligible" | "not_eligible"
    mappability_status: str  # "mappable" | "unresolvable_token_id_only" | "missing_identifier" | "sample_skipped"
    metadata_lookup_identifier_used: Optional[str]
    market_active_available: bool
    market_active_value: Optional[bool]
    market_closed_available: bool
    market_closed_value: Optional[bool]
    market_resolved_available: bool
    market_resolved_value: Optional[bool]
    market_end_time_available: bool
    market_end_time_value: Optional[str]
    seconds_to_market_end_available: bool
    seconds_to_market_end_value: Optional[int]
    market_identifier_mapping_status: str
    metadata_fetched_at: Optional[str]
    pr24u_pr24s_combinable: bool
    pr24u_pr24s_combination_note: Optional[str]
    skip_reason: Optional[str] = None
    error_reason: Optional[str] = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_trade_id": self.source_trade_id,
            "wallet_address": self.wallet_address,
            "market_source_id": self.market_source_id,
            "token_id": self.token_id,
            "side": self.side,
            "eligibility_status": self.eligibility_status,
            "mappability_status": self.mappability_status,
            "metadata_lookup_identifier_used": self.metadata_lookup_identifier_used,
            "market_active_available": self.market_active_available,
            "market_active_value": self.market_active_value,
            "market_closed_available": self.market_closed_available,
            "market_closed_value": self.market_closed_value,
            "market_resolved_available": self.market_resolved_available,
            "market_resolved_value": self.market_resolved_value,
            "market_end_time_available": self.market_end_time_available,
            "market_end_time_value": self.market_end_time_value,
            "seconds_to_market_end_available": self.seconds_to_market_end_available,
            "seconds_to_market_end_value": self.seconds_to_market_end_value,
            "market_identifier_mapping_status": self.market_identifier_mapping_status,
            "metadata_fetched_at": self.metadata_fetched_at,
            "pr24u_pr24s_combinable": self.pr24u_pr24s_combinable,
            "pr24u_pr24s_combination_note": self.pr24u_pr24s_combination_note,
            "skip_reason": self.skip_reason,
            "error_reason": self.error_reason,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TradeCopyabilityMarketStateFinding:
    """A single PR24V finding (mirrors PR24U finding shape)."""

    key: str
    severity: str  # "info" | "warning" | "blocker"
    summary: str
    count: Optional[int] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "summary": self.summary,
            "count": self.count,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class TradeCopyabilityMarketStateBridgeReport:
    """Full PR24V market-state evidence bridge report (read-only / dry-run)."""

    ready_to_wire_to_automation: bool
    ready_to_persist_decisions: bool
    ready_to_create_candidates: bool
    production_counts: dict[str, Any]
    source_trade_count: int
    eligible_count: int
    ineligible_count: int
    mappable_count: int
    unmappable_count: int
    resolved_count: int
    market_state_available_count: int
    end_time_available_count: int
    seconds_available_count: int
    raw_side_distribution: dict[str, int]
    canonical_side_distribution: dict[str, int]
    ingestion_side_inconsistency_present: bool
    live_preview_enabled: bool
    mapping_status_counts: dict[str, int]
    skip_reason_counts: dict[str, int]
    sample_like_row_count: int
    db_path_inspected: Optional[str]
    row_reports: tuple[TradeCopyabilityMarketStateRowReport, ...]
    findings: tuple[TradeCopyabilityMarketStateFinding, ...]
    recommended_next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready_to_wire_to_automation": self.ready_to_wire_to_automation,
            "ready_to_persist_decisions": self.ready_to_persist_decisions,
            "ready_to_create_candidates": self.ready_to_create_candidates,
            "production_counts": self.production_counts,
            "source_trade_count": self.source_trade_count,
            "eligible_count": self.eligible_count,
            "ineligible_count": self.ineligible_count,
            "mappable_count": self.mappable_count,
            "unmappable_count": self.unmappable_count,
            "resolved_count": self.resolved_count,
            "market_state_available_count": self.market_state_available_count,
            "end_time_available_count": self.end_time_available_count,
            "seconds_available_count": self.seconds_available_count,
            "raw_side_distribution": self.raw_side_distribution,
            "canonical_side_distribution": self.canonical_side_distribution,
            "ingestion_side_inconsistency_present": self.ingestion_side_inconsistency_present,
            "live_preview_enabled": self.live_preview_enabled,
            "mapping_status_counts": self.mapping_status_counts,
            "skip_reason_counts": self.skip_reason_counts,
            "sample_like_row_count": self.sample_like_row_count,
            "db_path_inspected": self.db_path_inspected,
            "row_reports": [r.to_dict() for r in self.row_reports],
            "findings": [f.to_dict() for f in self.findings],
            "recommended_next_step": self.recommended_next_step,
        }


# ── DB helpers (read-only; caller owns the connection) ─────────────────────
def _conn(conn_or_db: Any) -> sqlite3.Connection:
    if isinstance(conn_or_db, sqlite3.Connection):
        return conn_or_db
    maybe_conn = getattr(conn_or_db, "conn", None)
    if isinstance(maybe_conn, sqlite3.Connection):
        return maybe_conn
    raise TypeError("conn_or_db must be a sqlite3.Connection or Database-like object")


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _maybe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def _to_iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        return dt
    try:
        return str(dt)
    except Exception:
        return None


# A Gamma condition_id is a 0x-prefixed hex string (>= 8 hex chars). Used to
# decide whether an identifier is plausibly resolvable via Gamma get_market.
_CONDITION_ID_RE = __import__("re").compile(r"^0x[0-9a-fA-F]{8,}$")


def _looks_like_condition_id(value: Optional[str]) -> bool:
    return bool(value) and bool(_CONDITION_ID_RE.match(str(value).strip()))


# ── Field discovery (reuse PR24U field map; add condition_id / market_id) ──
_SOURCE_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "source_trade_id": ("source_trade_id", "id"),
    "trader_address": ("trader_address", "wallet_address", "wallet_id"),
    "wallet_id": ("wallet_id", "wallet_address", "trader_address"),
    "market_source_id": ("market_source_id", "market_id", "condition_id"),
    "token_id": ("token_id", "outcome_token_id", "clob_token_id",
                 "condition_id", "market_id"),
    "side": ("side", "trade_side"),
    "price": ("price", "source_price", "entry_price", "avg_price"),
    "size": ("size", "shares", "amount", "notional", "usd_size", "stake", "quantity"),
    "timestamp": ("timestamp", "created_at", "traded_at", "source_trade_timestamp"),
}


def _pick_local(row: sqlite3.Row, logical: str, field_map: dict[str, Optional[str]]) -> Any:
    col = field_map.get(logical)
    if col and col in row.keys():
        return row[col]
    for candidate in _SOURCE_FIELD_CANDIDATES.get(logical, ()):
        if candidate in row.keys():
            return row[candidate]
    return None


def _local_resolve_field_map(conn: sqlite3.Connection) -> dict[str, Optional[str]]:
    cols: set[str] = set()
    if _table_exists(conn, "source_trades"):
        for r in conn.execute("PRAGMA table_info(source_trades)"):
            cols.add(r["name"])
    mapping: dict[str, Optional[str]] = {}
    for logical, candidates in _SOURCE_FIELD_CANDIDATES.items():
        chosen = next((c for c in candidates if c in cols), None)
        mapping[logical] = chosen
    return mapping


# ── Identifier resolution ───────────────────────────────────────────────────
def _resolve_lookup_identifier(
    row: sqlite3.Row,
    field_map: dict[str, Optional[str]],
) -> tuple[Optional[str], str, str, Optional[str]]:
    """Resolve the best market-metadata lookup identifier for a row.

    Returns (identifier, identifier_kind, mapping_status, skip_reason).

    Priority: market_source_id > condition_id > market_id (all are conditionId
    shaped in this schema). token_id alone cannot key Gamma ``get_market``
    (which needs a conditionId), so a row with only token_id is reported
    honestly as ``unresolvable_token_id_only``.
    """
    market_source_id = _pick_local(row, "market_source_id", field_map)
    token_id = _pick_local(row, "token_id", field_map)

    if market_source_id and _looks_like_condition_id(market_source_id):
        return (str(market_source_id), "market_source_id",
                "resolved_via_market_source_id", None)
    if market_source_id and _norm_text(market_source_id):
        # Present but not conditionId-shaped (e.g. a sample/id placeholder).
        return (str(market_source_id), "market_source_id",
                "unresolvable_non_condition_id", "market_identifier_not_condition_id")

    # No usable market_source_id. token_id cannot be resolved by Gamma
    # get_market without a token->condition mapping helper (deferred).
    if token_id and _norm_text(token_id):
        return (None, "token_id_only",
                "unresolvable_token_id_only",
                "token_id_cannot_resolve_market_state_without_condition_mapping")
    return (None, "none", "missing_identifier", "missing_market_identifier")


# ── Normalize a fetched market into report fields ───────────────────────────
def _normalize_market_state(
    result: _MarketStateFetchResult,
    now: datetime,
) -> dict[str, Any]:
    """Turn a fetch result into honest report field values.

    Market state is NEVER invented when unavailable. ``seconds_to_market_end``
    is computed ONLY when an end_date is present, and negative values (past
    close) are reported honestly, not clamped.
    """
    market = result.market
    out: dict[str, Any] = {
        "market_active_available": False,
        "market_active_value": None,
        "market_closed_available": False,
        "market_closed_value": None,
        "market_resolved_available": False,
        "market_resolved_value": None,
        "market_end_time_available": False,
        "market_end_time_value": None,
        "seconds_to_market_end_available": False,
        "seconds_to_market_end_value": None,
        "metadata_fetched_at": _to_iso(result.fetched_at),
        "error_code": result.error_code,
        "error_message": result.error_message,
        "notes": [],
    }
    if market is None:
        # Not fetched (offline), not found (404), or errored: report nothing
        # invented. Availability stays False; values stay None.
        if result.fetched and result.error_code == "MARKET_NOT_FOUND":
            out["notes"].append("Gamma get_market returned no market (unknown condition_id)")
        elif result.fetched and result.error_code == "FETCH_ERROR":
            out["notes"].append(f"Gamma fetch error captured (controlled): {result.error_message}")
        elif not result.fetched:
            out["notes"].append("offline dry-run; no Gamma fetch performed")
        return out

    # A real market object is present.
    active = getattr(market, "active", None)
    closed = getattr(market, "closed", None)
    resolved = getattr(market, "resolved", None)
    end_date = getattr(market, "end_date", None)

    out["market_active_available"] = active is not None
    out["market_active_value"] = bool(active) if active is not None else None
    out["market_closed_available"] = closed is not None
    out["market_closed_value"] = bool(closed) if closed is not None else None
    out["market_resolved_available"] = resolved is not None
    out["market_resolved_value"] = bool(resolved) if resolved is not None else None

    if end_date is not None:
        out["market_end_time_available"] = True
        out["market_end_time_value"] = _to_iso(end_date)
        try:
            seconds = int((end_date - now).total_seconds())
        except Exception:
            seconds = None
        if seconds is not None:
            out["seconds_to_market_end_available"] = True
            out["seconds_to_market_end_value"] = seconds
            if seconds < 0:
                out["notes"].append(
                    f"market end time is in the past; seconds_to_market_end={seconds} (negative, reported honestly)"
                )
    else:
        out["notes"].append("market end_date absent in Gamma payload; seconds_to_market_end not computed")

    if closed is True:
        out["notes"].append("market reported closed=True")
    if resolved is True:
        out["notes"].append("market reported resolved=True")
    return out


# ── Per-row audit (builds the canonical report row) ────────────────────────
def _audit_row(
    row: sqlite3.Row,
    field_map: dict[str, Optional[str]],
    provider: MarketStateProvider,
    *,
    live_preview: bool,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> TradeCopyabilityMarketStateRowReport:
    raw_side = _pick_local(row, "side", field_map)
    canonical_side, side_status, side_reason = canonicalize_source_side(raw_side)
    source_trade_id = _pick_local(row, "source_trade_id", field_map)
    wallet_address = _pick_local(row, "trader_address", field_map)
    market_source_id = _pick_local(row, "market_source_id", field_map)
    token_id = _pick_local(row, "token_id", field_map)
    source_price = _maybe_float(_pick_local(row, "price", field_map))
    source_size = _maybe_float(_pick_local(row, "size", field_map))

    notes: list[str] = []
    skip_reasons: list[str] = []
    error_reason: Optional[str] = None

    # --- Input eligibility (PR24S/PR24U logic) ---
    eligible = True
    if canonical_side != "BUY":
        eligible = False
        if side_reason == SELL_UNSUPPORTED_REASON:
            skip_reasons.append(SELL_UNSUPPORTED_REASON)
        elif side_status == "missing":
            skip_reasons.append("missing_side")
        else:
            skip_reasons.append("invalid_side")
    if source_trade_id in (None, ""):
        eligible = False
        skip_reasons.append("missing_source_trade_id")
    if (token_id in (None, "")) and (market_source_id in (None, "")):
        eligible = False
        skip_reasons.append("missing_token_id_and_market_source_id")
    if token_id in (None, ""):
        skip_reasons.append("missing_token_id")
    if market_source_id in (None, ""):
        # Not necessarily ineligible if token_id present, but flagged.
        pass
    if source_price is None:
        eligible = False
        skip_reasons.append("missing_source_entry_price")
    if source_size is None:
        eligible = False
        skip_reasons.append("missing_source_size")

    # --- Identifier / mappability resolution ---
    (
        lookup_id,
        id_kind,
        mapping_status,
        id_skip,
    ) = _resolve_lookup_identifier(row, field_map)

    if _row_looks_sample_like(row):
        mappability_status = "sample_skipped"
        if id_skip:
            skip_reasons.append(id_skip)
    elif mapping_status == "resolved_via_market_source_id":
        mappability_status = "mappable"
    else:
        mappability_status = mapping_status  # unresolvable_* / missing_identifier
        if id_skip:
            skip_reasons.append(id_skip)

    # Defaults (not-resolved path).
    active_av = active_val = closed_av = closed_val = None
    resolved_av = resolved_val = end_av = end_val = None
    secs_av = secs_val = None
    metadata_fetched_at: Optional[str] = None

    # Attempt metadata resolution only when there is a usable lookup id that
    # is NOT a sample row and the identifier looks resolvable.
    if (
        lookup_id
        and not _row_looks_sample_like(row)
        and mapping_status == "resolved_via_market_source_id"
    ):
        try:
            if loop is not None:
                result = loop.run_until_complete(
                    provider.fetch_market_state(condition_id=lookup_id)
                )
            else:
                result = asyncio.run(
                    provider.fetch_market_state(condition_id=lookup_id)
                )
        except Exception as exc:  # controlled: never crash the whole run
            error_reason = f"{type(exc).__name__}: {exc}"[:300]
            result = _MarketStateFetchResult(
                condition_id=lookup_id,
                fetched=live_preview,
                market=None,
                error_code="PROVIDER_RAISED",
                error_message=error_reason,
            )

        norm = _normalize_market_state(result, datetime.now(timezone.utc))
        active_av = norm["market_active_available"]
        active_val = norm["market_active_value"]
        closed_av = norm["market_closed_available"]
        closed_val = norm["market_closed_value"]
        resolved_av = norm["market_resolved_available"]
        resolved_val = norm["market_resolved_value"]
        end_av = norm["market_end_time_available"]
        end_val = norm["market_end_time_value"]
        secs_av = norm["seconds_to_market_end_available"]
        secs_val = norm["seconds_to_market_end_value"]
        metadata_fetched_at = norm["metadata_fetched_at"]
        if norm.get("error_code") and norm["error_code"] not in ("OFFLINE_NO_FETCH", None):
            error_reason = f"{norm['error_code']}: {norm.get('error_message')}"[:300]
        notes.extend(norm.get("notes", []))

    else:
        notes.append(
            "no resolvable market identifier present; market-state metadata "
            "could not be fetched (honest: not invented)"
        )

    # PR24U/PR24S combination: those structures already carry
    # market_active/closed/resolved/seconds_to_market_end fields (currently
    # None). If a lookup identifier is mappable, the produced metadata CAN be
    # merged into them; otherwise explain why.
    if mappability_status == "mappable":
        combinable = True
        combo_note = (
            "market_active/closed/resolved/end_time/seconds_to_market_end can be "
            "merged into PR24S SnapshotEvidenceResult and PR24U row report (both "
            "already carry these fields, currently None)."
        )
    elif mappability_status == "sample_skipped":
        combinable = False
        combo_note = "sample/placeholder row; excluded from any combination (report-only)."
    elif mappability_status == "unresolvable_token_id_only":
        combinable = False
        combo_note = (
            "token_id alone cannot map to market metadata without a "
            "token->condition_id helper (deferred); PR24U/PR24S combination blocked."
        )
    else:
        combinable = False
        combo_note = "no resolvable market identifier; cannot combine with PR24U/PR24S."

    return TradeCopyabilityMarketStateRowReport(
        source_trade_id=source_trade_id if source_trade_id not in (None, "") else None,
        wallet_address=wallet_address if wallet_address not in (None, "") else None,
        market_source_id=market_source_id if market_source_id not in (None, "") else None,
        token_id=token_id if token_id not in (None, "") else None,
        side=raw_side if raw_side != "" else None,
        eligibility_status="eligible" if eligible else "not_eligible",
        mappability_status=mappability_status,
        metadata_lookup_identifier_used=lookup_id,
        market_active_available=bool(active_av),
        market_active_value=active_val,
        market_closed_available=bool(closed_av),
        market_closed_value=closed_val,
        market_resolved_available=bool(resolved_av),
        market_resolved_value=resolved_val,
        market_end_time_available=bool(end_av),
        market_end_time_value=end_val,
        seconds_to_market_end_available=bool(secs_av),
        seconds_to_market_end_value=secs_val,
        market_identifier_mapping_status=mapping_status,
        metadata_fetched_at=metadata_fetched_at,
        pr24u_pr24s_combinable=combinable,
        pr24u_pr24s_combination_note=combo_note,
        skip_reason=";".join(sorted(set(skip_reasons))) if skip_reasons else None,
        error_reason=error_reason,
        notes=tuple(notes),
    )


# ── Report builder ──────────────────────────────────────────────────────────
def build_trade_copyability_market_state_evidence_bridge(
    conn_or_db: Any,
    *,
    limit: int = 20,
    provider: Optional[MarketStateProvider] = None,
    live_preview: bool = False,
    db_path: Optional[str] = None,
) -> TradeCopyabilityMarketStateBridgeReport:
    """Build a read-only Trade Copyability MARKET-STATE evidence report.

    ``conn_or_db`` must be an already-open ``sqlite3.Connection`` opened
    read-only (``mode=ro``). The function performs only SELECT / PRAGMA reads
    and never mutates the database. Metadata resolution is performed through the
    injected ``provider`` (offline default; live Gamma only when
    ``live_preview=True`` AND a live provider was injected).
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if provider is None:
        provider = OfflineMarketStateProvider()  # offline default

    conn = _conn(conn_or_db)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        source_rows: list[sqlite3.Row] = []
        if _table_exists(conn, "source_trades"):
            source_rows = list(conn.execute("SELECT * FROM source_trades"))

        production_counts = {t: _safe_count(conn, t) for t in _COUNT_TABLES}
        field_map = _local_resolve_field_map(conn)

        row_reports: list[TradeCopyabilityMarketStateRowReport] = []
        for row in source_rows:
            rr = _audit_row(
                row, field_map, provider, live_preview=live_preview, loop=loop
            )
            row_reports.append(rr)

        sample_like_row_count = sum(1 for row in source_rows if _row_looks_sample_like(row))

        # --- Distributions ---
        raw_side_distribution: Counter[str] = Counter()
        canonical_side_distribution: Counter[str] = Counter()
        for rr in row_reports:
            key = "<NULL>" if rr.side is None else str(rr.side)
            raw_side_distribution[key] += 1
            if rr.side is not None:
                cs, _, _ = canonicalize_source_side(rr.side)
                if cs is not None:
                    canonical_side_distribution[cs] += 1

        ingestion_inconsistent = (
            sum(1 for k in raw_side_distribution if k.lower() == "buy") > 1
            or sum(1 for k in raw_side_distribution if k.lower() == "sell") > 1
        )

        eligible = [rr for rr in row_reports if rr.eligibility_status == "eligible"]
        ineligible = [rr for rr in row_reports if rr.eligibility_status != "eligible"]
        mappable = [rr for rr in row_reports if rr.mappability_status == "mappable"]
        unmappable = [rr for rr in row_reports if rr.mappability_status != "mappable"]

        # "resolved" = actually attempted a fetch AND got market state fields.
        resolved = [
            rr for rr in row_reports
            if rr.metadata_fetched_at is not None
            and (rr.market_active_available or rr.market_closed_available
                 or rr.market_resolved_available)
        ]
        market_state_available_count = sum(
            1 for rr in row_reports if rr.market_active_available
            or rr.market_closed_available or rr.market_resolved_available
        )
        end_time_available_count = sum(1 for rr in row_reports if rr.market_end_time_available)
        seconds_available_count = sum(1 for rr in row_reports if rr.seconds_to_market_end_available)

        mapping_status_counts: Counter[str] = Counter()
        for rr in row_reports:
            mapping_status_counts[rr.mappability_status] += 1

        skip_reason_counts: Counter[str] = Counter()
        for rr in row_reports:
            if rr.skip_reason:
                for part in rr.skip_reason.split(";"):
                    skip_reason_counts[part] += 1

        # --- Findings ---
        findings: list[TradeCopyabilityMarketStateFinding] = []
        if ingestion_inconsistent:
            findings.append(TradeCopyabilityMarketStateFinding(
                key="ingestion_side_inconsistency",
                severity="warning",
                summary=(
                    "source_trades.side contains multiple exact string forms for the "
                    "same logical side (e.g. buy vs BUY). PR24T normalization guard "
                    "handles future writes; existing production rows were intentionally "
                    "not backfilled. This PR does not normalize them."
                ),
                count=sum(raw_side_distribution.get(k, 0)
                          for k in raw_side_distribution if k.lower() in ("buy", "sell")),
                evidence={
                    "raw_side_distribution": dict(raw_side_distribution),
                    "canonical_side_distribution": dict(canonical_side_distribution),
                },
                recommendation=(
                    "Leave existing rows as-is (no backfill per PR24T). Future writes "
                    "normalize via normalize_side_for_persistence."
                ),
            ))

        if sample_like_row_count:
            effective_real_n = len(row_reports) - sample_like_row_count
            findings.append(TradeCopyabilityMarketStateFinding(
                key="source_trade_sample_data_present",
                severity="info",
                summary=(
                    f"Of {len(row_reports)} source_trades inspected, {sample_like_row_count} "
                    "appear to be seeded/sample/placeholder rows (wallet/market/trade_id "
                    "contain sample markers such as 0xsample_trader_*_do_not_use and "
                    "sample-market-*). Real usable production-like market-state coverage is "
                    f"effectively n={max(effective_real_n, 0)}. This is report text only; "
                    "the rows are NOT deleted, mutated, backfilled, or normalized."
                ),
                count=sample_like_row_count,
                evidence={
                    "source_trade_count": len(row_reports),
                    "sample_like_row_count": sample_like_row_count,
                    "effective_real_coverage_n": max(effective_real_n, 0),
                },
                recommendation=(
                    "Treat the single non-sample mappable row (test_trade_1) as the only "
                    "currently-real market-state resolution target. A future ingestion PR "
                    "should populate real rows with conditionId-shaped market_source_id for "
                    "broader coverage."
                ),
            ))

        if mapping_status_counts:
            findings.append(TradeCopyabilityMarketStateFinding(
                key="identifier_mapping_status_summary",
                severity="info",
                summary=(
                    "Per-row market-identifier mapping status. 'mappable' means a "
                    "conditionId-shaped market_source_id is present and resolvable via Gamma "
                    "get_market. 'unresolvable_token_id_only' means only token_id is present "
                    "(Gamma get_market keys on conditionId; a deferred token->condition helper "
                    "is required). 'sample_skipped' / 'missing_identifier' are excluded."
                ),
                count=sum(mapping_status_counts.values()),
                evidence={"mapping_status_counts": dict(mapping_status_counts)},
                recommendation=(
                    "Persist/ingest a conditionId-shaped market_source_id per real trade; add "
                    "a token->condition mapping helper if token_id-only resolution is needed."
                ),
            ))

        if live_preview:
            findings.append(TradeCopyabilityMarketStateFinding(
                key="live_preview_market_state_fetched",
                severity="info",
                summary=(
                    "Live Gamma get_market fetch PROVIDED market_active/closed/resolved + "
                    "end_date + seconds_to_market_end for resolvable conditionIds. Market "
                    "state was fetched read-only and is NOT invented for non-fetched rows."
                ),
                count=market_state_available_count,
                evidence={
                    "market_state_available_count": market_state_available_count,
                    "end_time_available_count": end_time_available_count,
                    "seconds_available_count": seconds_available_count,
                    "resolved_count": len(resolved),
                },
                recommendation=(
                    "A future persistence PR can land these fields into PR24S/PR24U structures."
                ),
            ))

        if not live_preview:
            findings.append(TradeCopyabilityMarketStateFinding(
                key="dry_run_offline_no_network",
                severity="info",
                summary=(
                    "Default dry-run mode performed NO network fetch. Mappability and "
                    "field-availability were proven structurally against source_trades. "
                    "Use --allow-live-preview to perform a real read-only Gamma get_market "
                    "fetch (still non-persisting)."
                ),
                recommendation="Run with --allow-live-preview for a live metadata preview.",
            ))

        recommended_next_step = (
            "PR24V proves the market-state / end-time evidence path is wireable by REUSING "
            "PolymarketPublicAdapter.get_market (Gamma /markets/{conditionId}) and reports "
            "which identifiers can resolve. Next: (a) a guarded persistence writer landing "
            "these fields into PR24S SnapshotEvidenceResult + PR24U row report (both already "
            "carry them, currently None), and (b) a token->condition mapping helper for "
            "token_id-only rows. Do not wire automation or persist decisions until those land "
            "and are reviewed."
        )

        return TradeCopyabilityMarketStateBridgeReport(
            ready_to_wire_to_automation=False,
            ready_to_persist_decisions=False,
            ready_to_create_candidates=False,
            production_counts=production_counts,
            source_trade_count=len(row_reports),
            eligible_count=len(eligible),
            ineligible_count=len(ineligible),
            mappable_count=len(mappable),
            unmappable_count=len(unmappable),
            resolved_count=len(resolved),
            market_state_available_count=market_state_available_count,
            end_time_available_count=end_time_available_count,
            seconds_available_count=seconds_available_count,
            raw_side_distribution=dict(raw_side_distribution),
            canonical_side_distribution=dict(canonical_side_distribution),
            ingestion_side_inconsistency_present=ingestion_inconsistent,
            live_preview_enabled=live_preview,
            mapping_status_counts=dict(mapping_status_counts),
            skip_reason_counts=dict(skip_reason_counts),
            sample_like_row_count=sample_like_row_count,
            db_path_inspected=db_path,
            row_reports=tuple(row_reports[: max(limit, 0)] or row_reports),
            findings=tuple(findings),
            recommended_next_step=recommended_next_step,
        )
    finally:
        conn.row_factory = old_factory
        if loop is not None:
            loop.close()


# ── Human rendering ──────────────────────────────────────────────────────────
def report_to_human(report: TradeCopyabilityMarketStateBridgeReport) -> str:
    lines: list[str] = []
    lines.append("TRADE COPYABILITY MARKET-STATE / END-TIME EVIDENCE BRIDGE — "
                 "READ ONLY / DRY RUN / REPORT-ONLY")
    lines.append("")
    lines.append(f"ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")
    lines.append(f"ready_to_persist_decisions = {report.ready_to_persist_decisions}")
    lines.append(f"ready_to_create_candidates = {report.ready_to_create_candidates}")
    lines.append(f"live_preview_enabled = {report.live_preview_enabled}")
    lines.append("")
    lines.append(f"DB path inspected: {report.db_path_inspected}")
    lines.append("")
    lines.append("== Production counts (read-only; must be unchanged by this PR) ==")
    for k, v in report.production_counts.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"== Source trades inspected: {report.source_trade_count} ==")
    lines.append(f"  eligible_for_bridge: {report.eligible_count}")
    lines.append(f"  ineligible_for_bridge: {report.ineligible_count}")
    lines.append(f"  mappable_via_market_source_id: {report.mappable_count}")
    lines.append(f"  unmappable: {report.unmappable_count}")
    lines.append(f"  resolved_market_metadata: {report.resolved_count}")
    if report.sample_like_row_count:
        effective = max(report.source_trade_count - report.sample_like_row_count, 0)
        lines.append(
            f"  sample_like_rows: {report.sample_like_row_count} "
            f"(seeded/placeholder; NOT deleted/mutated/backfilled)"
        )
        lines.append(f"  effective_real_production_like_coverage: n={effective}")
    lines.append("")
    lines.append("== Metadata field availability ==")
    lines.append(f"  market_state_available_count: {report.market_state_available_count}")
    lines.append(f"  end_time_available_count: {report.end_time_available_count}")
    lines.append(f"  seconds_to_market_end_available_count: {report.seconds_available_count}")
    lines.append("")
    lines.append("== Raw source side distribution (exact casing) ==")
    for k, v in sorted(report.raw_side_distribution.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("== Canonical side distribution ==")
    if report.canonical_side_distribution:
        for k, v in sorted(report.canonical_side_distribution.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("== INGESTION SIDE NORMALIZATION AUDIT ==")
    lines.append(f"  ingestion_side_inconsistency_present = {report.ingestion_side_inconsistency_present}")
    if report.ingestion_side_inconsistency_present:
        lines.append("  NOTE: mixed casing present; NOT backfilled (PR24T left existing rows).")
    lines.append("")
    lines.append("== Identifier mapping status ==")
    if report.mapping_status_counts:
        for k, v in sorted(report.mapping_status_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("== Skip reasons (rows) ==")
    if report.skip_reason_counts:
        for k, v in sorted(report.skip_reason_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("== Findings ==")
    if report.findings:
        for f in report.findings:
            lines.append(f"  [{f.severity}] {f.key}: {f.summary}")
            if f.count is not None:
                lines.append(f"    count={f.count}")
            if f.evidence:
                lines.append(f"    evidence={json.dumps(f.evidence, sort_keys=True)}")
            if f.recommendation:
                lines.append(f"    -> {f.recommendation}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("== Per-row report (exact PR24V required fields) ==")
    for rr in report.row_reports:
        lines.append(
            f"  source_trade_id={rr.source_trade_id} wallet={rr.wallet_address} "
            f"market={rr.market_source_id} token={rr.token_id} side={rr.side}"
        )
        lines.append(
            f"    eligibility={rr.eligibility_status} mappability={rr.mappability_status} "
            f"lookup_id={rr.metadata_lookup_identifier_used}"
        )
        lines.append(
            f"    market_active={rr.market_active_available}:{rr.market_active_value} "
            f"closed={rr.market_closed_available}:{rr.market_closed_value} "
            f"resolved={rr.market_resolved_available}:{rr.market_resolved_value}"
        )
        lines.append(
            f"    end_time={rr.market_end_time_available}:{rr.market_end_time_value} "
            f"seconds_to_end={rr.seconds_to_market_end_available}:{rr.seconds_to_market_end_value} "
            f"fetched_at={rr.metadata_fetched_at}"
        )
        lines.append(
            f"    mapping_status={rr.market_identifier_mapping_status} "
            f"pr24u_pr24s_combinable={rr.pr24u_pr24s_combinable}"
        )
        if rr.skip_reason:
            lines.append(f"    skip_reason={rr.skip_reason}")
        if rr.error_reason:
            lines.append(f"    error_reason={rr.error_reason}")
        for n in rr.notes:
            lines.append(f"    note: {n}")
    lines.append("")
    lines.append("== Recommended next step ==")
    lines.append(f"  {report.recommended_next_step}")
    lines.append("")
    lines.append(
        "This report performs NO production writes: no market-state persistence, decisions, "
        "candidates, paper signals, orders, or positions. Default mode is dry-run. Live "
        "preview (--allow-live-preview) is read-only and non-persisting."
    )
    return "\n".join(lines)


def report_to_dict(report: TradeCopyabilityMarketStateBridgeReport) -> dict[str, Any]:
    return report.to_dict()


__all__ = [
    "OfflineMarketStateProvider",
    "LiveGammaMarketStateProvider",
    "MarketStateProvider",
    "TradeCopyabilityMarketStateRowReport",
    "TradeCopyabilityMarketStateFinding",
    "TradeCopyabilityMarketStateBridgeReport",
    "build_trade_copyability_market_state_evidence_bridge",
    "report_to_human",
    "report_to_dict",
]
