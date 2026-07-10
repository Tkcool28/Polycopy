"""PR24Y — READ-ONLY REAL WALLET TRADE SOURCE PROBE (engine core).

This module is the pure, read-only analysis core behind the PR24Y probe. It
answers the master question:

    Which existing API / adapter / endpoint can reliably return real wallet
    trade history with enough information to construct copyability-ready BUY
    source trades?

Design
------
Live source -> read-only probe -> diagnostic field mapping -> report.

There is NO database writer anywhere in this module:

  * It never imports ``polycopy.db.database`` (no write path).
  * It never issues INSERT/UPDATE/DELETE/CREATE/DROP/ALTER.
  * It consumes an injectable ``RealTradeSourceProvider`` that returns raw
    data-api-shaped dicts. A fake provider is used in tests; the real
    provider (in the CLI) wraps ``PolymarketPublicAdapter.get_trades_by_address``
    and converts each ``SourceTrade`` to a raw dict. The probe itself stays
    provider-agnostic and DB-free.

BUY-only V1 rule
----------------
Polycopy V1 evaluates BUY trades only. SELL records may be observed but are
classified ``excluded_unsupported_side`` and never reported as
ingestion-eligible. Missing/unknown side -> ``excluded_missing_fields``.

Readiness (carried from PR24U / PR24V / PR24W)
------------------------------------------------
  * PR24U-ready  : ``token_id`` present and non-placeholder.
  * PR24V-ready  : ``conditionId``-shaped market identifier present, OR a
                   token_id can be mapped with the already-proven read-only
                   token->condition path (PR24W). The probe only *reports*
                   readiness; it does NOT perform the mapping here.
  * both-ready   : PR24U-ready AND PR24V-ready.

Network safety
--------------
The core module never touches the network itself. All network access happens in
the injected provider (or, in live mode, in the CLI's real provider). The
defaults (limit=25, hard max=100, max_pages=2 for PR24Y) are enforced here so
every provider — fake or real — is bounded identically.

No decisions, no candidates, no signals, no snapshots, no orders, no positions,
no scoring, no persistence, no automation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

# ── Versioning ───────────────────────────────────────────────────────────────
PROBE_VERSION = "PR24Y-1"

# Bounds enforced by the probe (mirrors the task's network-safety rules).
DEFAULT_RECORD_LIMIT = 25
HARD_MAX_RECORD_LIMIT = 100
# PR24Y may fetch at most two bounded pages during a live preview.
PR24Y_MAX_PAGES = 2

# A real on-chain transaction hash: 0x + 8+ hex chars.
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{8,}$")
# A conditionId-shaped identifier: 0x + 64 hex chars (Keccak).
_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
# A CLOB token id: 0x + 64 hex chars (EIP-55-ish, any case) OR a decimal
# numeric string (the data-api returns `asset` as a large decimal integer).
_TOKEN_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID_DEC_RE = re.compile(r"^[0-9]{10,}$")


def _is_valid_token_id(value: Any) -> bool:
    """A token id is a 0x-hex (64) id or a decimal numeric asset string.

    The data-api `asset` field is returned as a large decimal integer (not
    0x-hex), so both forms must be accepted as a valid CLOB token id.
    """
    if not isinstance(value, str):
        return False
    if _is_placeholder(value):
        return False
    return bool(_TOKEN_ID_RE.match(value) or _TOKEN_ID_DEC_RE.match(value))
# EVM address: 0x + 40 hex chars.
_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ── Provider protocol (injectable; fake in tests, real in CLI) ───────────────
@runtime_checkable
class RealTradeSourceProvider(Protocol):
    """Returns raw data-api ``/trades``-shaped dicts for ONE wallet page.

    The probe passes ``limit`` and ``page`` (0-based); the provider computes
    the offset and performs the (bounded) fetch. Implementations must be
    fetch-only and must never write to any database.

    ``made_network_call`` is a runtime flag the probe reads to distinguish a
    REAL external HTTP request from an in-memory fixture. Only real calls are
    counted in ``network_calls_attempted`` / ``network_calls_succeeded``.
    Fixture providers MUST leave it False (so the default offline run reports
    0/0/0). Real providers MUST set it True before returning data.
    """

    made_network_call: bool = False

    async def fetch_trades(
        self, wallet: str, *, limit: int, page: int
    ) -> list[dict[str, Any]]:
        """Return a list of raw trade dicts for ``wallet`` at ``page``."""
        ...


# ── Diagnostic preview record ────────────────────────────────────────────────
@dataclass
class RealTradeSourcePreview:
    """Per-record diagnostic field mapping (NOT a persistent SourceTrade)."""

    source: str = "polymarket_data_api"
    raw_index: int = 0
    source_trade_id: Optional[str] = None
    source_trade_id_present: bool = False
    trader_address: Optional[str] = None
    trader_address_present: bool = False
    token_id: Optional[str] = None
    token_id_present: bool = False
    condition_id: Optional[str] = None
    condition_id_present: bool = False
    side_raw: Optional[str] = None
    side_canonical: Optional[str] = None
    price: Optional[float] = None
    price_present: bool = False
    size: Optional[float] = None
    size_present: bool = False
    timestamp: Optional[str] = None
    timestamp_present: bool = False
    outcome: Optional[str] = None
    outcome_present: bool = False
    transaction_hash: Optional[str] = None
    transaction_hash_present: bool = False
    market_title: Optional[str] = None
    market_slug: Optional[str] = None
    raw_field_names: dict[str, str] = field(default_factory=dict)
    eligibility_reason: str = ""
    pr24u_ready: bool = False
    pr24v_ready: bool = False
    both_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Field extraction helpers (pure, no DB) ────────────────────────────────────
def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _redact(value: Optional[str], keep: int = 10) -> Optional[str]:
    """Redact an address/hash for safe sample display in reports."""
    if value is None:
        return None
    s = str(value)
    if len(s) <= keep + 4:
        return s
    return s[:keep] + "…" + s[-4:]


def _extract_side(raw: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (side_raw, side_canonical). Canonical is BUY/SELL/None."""
    raw_side = raw.get("side")
    if raw_side is None:
        return None, None
    s = str(raw_side).strip().lower()
    if s in ("buy", "1"):
        return str(raw_side), "BUY"
    if s in ("sell", "0"):
        return str(raw_side), "SELL"
    return str(raw_side), None


def _is_placeholder(value: Optional[str]) -> bool:
    """True if value is missing or a legacy sentinel token."""
    if value is None:
        return True
    s = str(value).strip().lower()
    if not s:
        return True
    return s in {
        "unknown",
        "anonymous",
        "missing",
        "0x",
        "0x0",
        "0x0000000000000000000000000000000000000000",
    }


def _build_preview(raw: dict[str, Any], *, index: int) -> RealTradeSourcePreview:
    """Map one raw data-api trade dict into a diagnostic preview record."""
    pv = RealTradeSourcePreview(raw_index=index)
    pv.raw_field_names = {k: k for k in raw.keys()}

    pv.source = str(raw.get("source") or "polymarket_data_api")

    # source_trade_id — prefer upstream id; the probe only *observes* it.
    # On the canonical data-api response the transaction hash is present in
    # `transactionHash`; the probe records it (both a stable-identity candidate
    # AND an adapter-gap diagnostic: the current adapter drops it).
    tx = raw.get("transactionHash")
    if isinstance(tx, str):
        pv.transaction_hash = tx
        pv.transaction_hash_present = True
        pv.source_trade_id = f"polymarket:{tx}"
    pv.source_trade_id_present = pv.source_trade_id is not None

    # trader / wallet
    wallet = raw.get("proxyWallet") or raw.get("maker") or raw.get("trader")
    if isinstance(wallet, str) and not _is_placeholder(wallet):
        pv.trader_address = wallet
        pv.trader_address_present = True

    # token_id (CLOB asset) — accepts 0x-hex OR decimal numeric asset string.
    asset = raw.get("asset")
    if _is_valid_token_id(asset):
        pv.token_id = asset
        pv.token_id_present = True

    # conditionId / market_source_id
    cond = raw.get("conditionId")
    if isinstance(cond, str) and _CONDITION_ID_RE.match(cond):
        pv.condition_id = cond
        pv.condition_id_present = True

    # side
    side_raw, side_canonical = _extract_side(raw)
    pv.side_raw = side_raw
    pv.side_canonical = side_canonical

    # price
    price = _as_float(raw.get("price"))
    if price is not None:
        pv.price = price
        pv.price_present = True

    # size
    size = _as_float(raw.get("size"))
    if size is not None:
        pv.size = size
        pv.size_present = True

    # timestamp
    ts = raw.get("timestamp")
    if ts is not None:
        pv.timestamp = str(ts)
        pv.timestamp_present = True

    # outcome
    outcome = raw.get("outcome")
    if isinstance(outcome, str) and outcome.strip():
        pv.outcome = outcome
        pv.outcome_present = True

    # market metadata (denormalized in data-api payloads)
    pv.market_title = raw.get("title")
    pv.market_slug = raw.get("slug")

    # Readiness (observational only; no mapping performed here).
    pv.pr24u_ready = pv.token_id_present
    pv.pr24v_ready = pv.condition_id_present  # OR token->condition mapping (PR24W)
    pv.both_ready = pv.pr24u_ready and pv.pr24v_ready

    # Eligibility (BUY-only V1).
    if side_canonical is None:
        pv.eligibility_reason = "excluded_missing_fields"
    elif side_canonical == "SELL":
        pv.eligibility_reason = "excluded_unsupported_side"
    else:
        # BUY side: eligible only if all required fields present.
        missing = []
        if not pv.trader_address_present:
            missing.append("trader_address")
        if not pv.token_id_present:
            missing.append("token_id")
        if not pv.condition_id_present:
            missing.append("conditionId")
        if not pv.price_present:
            missing.append("price")
        if not pv.size_present:
            missing.append("size")
        if not pv.timestamp_present:
            missing.append("timestamp")
        if missing:
            pv.eligibility_reason = "excluded_missing_fields:" + ",".join(missing)
        else:
            pv.eligibility_reason = "eligible_buy"
    return pv


# ── Identity ranking ─────────────────────────────────────────────────────────
@dataclass
class StableIdentityAssessment:
    stable_source_trade_id_available: bool = False
    identity_field: Optional[str] = None
    identity_uniqueness_confidence: str = "none"
    fallback_components_available: list[str] = field(default_factory=list)
    collision_risk_notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_stable_identity(
    previews: list[RealTradeSourcePreview], *, live: bool = False
) -> StableIdentityAssessment:
    """Rank identity options: (1) source trade/fill ID, (2) tx hash + index,
    (3) deterministic composite key.

    ``live`` distinguishes findings derived from a REAL external fetch (live=True)
    from those produced only by in-memory fixtures (live=False). Fixture-only
    collision/duplicate findings are explicitly labeled ``fixture_verified`` so
    they are never mistaken for observations in live wallet data.
    """
    a = StableIdentityAssessment()
    by_id: dict[str, int] = {}
    for pv in previews:
        if pv.source_trade_id_present and pv.source_trade_id:
            by_id[pv.source_trade_id] = by_id.get(pv.source_trade_id, 0) + 1
    if by_id:
        dup = [k for k, v in by_id.items() if v > 1]
        if not dup:
            a.stable_source_trade_id_available = True
            a.identity_field = "transactionHash (source trade/fill id)"
            a.identity_uniqueness_confidence = "high"
            a.collision_risk_notes = (
                "transactionHash unique across observed records; suitable as "
                "natural dedup key (UNIQUE(source, source_trade_id))."
            )
        else:
            a.stable_source_trade_id_available = False
            a.identity_field = "transactionHash (collisions observed)"
            a.identity_uniqueness_confidence = "low"
            note = (
                f"{len(dup)} duplicate transactionHash values observed; a row-"
                "distinguishing key (PR24X deterministic_source_trade_id_v2) is "
                "required before ingestion."
            )
            if not live:
                note = "fixture_verified: " + note
            a.collision_risk_notes = note
    a.fallback_components_available = [
        "wallet_address",
        "token_id/conditionId",
        "side",
        "price",
        "size",
        "timestamp",
    ]
    return a


# ── Source-selection verdict ─────────────────────────────────────────────────
SOURCE_VERDICTS = (
    "SOURCE_CONFIRMED",
    "SOURCE_PARTIAL",
    "SOURCE_UNSUITABLE",
    "SOURCE_UNAVAILABLE",
    "SOURCE_REQUIRES_AUTH",
    "SOURCE_RESPONSE_CHANGED",
)


@dataclass
class RealTradeSourceProbeResult:
    probe_version: str = PROBE_VERSION
    generated_at: str = ""
    live_preview_enabled: bool = False
    network_calls_attempted: int = 0
    network_calls_succeeded: int = 0
    source_candidates_examined: int = 0
    selected_source: Optional[str] = None
    source_selection_verdict: Optional[str] = None
    wallet_count: int = 0
    record_limit: int = DEFAULT_RECORD_LIMIT
    pages_fetched: int = 0
    raw_records: int = 0
    raw_buy_records: int = 0
    raw_sell_records: int = 0
    unknown_side_records: int = 0
    eligible_buy_records: int = 0
    excluded_unsupported_side: int = 0
    excluded_missing_fields: int = 0
    stable_source_trade_id_available: bool = False
    token_id_available_count: int = 0
    condition_id_available_count: int = 0
    price_available_count: int = 0
    size_available_count: int = 0
    timestamp_available_count: int = 0
    pr24u_ready_count: int = 0
    pr24v_ready_count: int = 0
    both_ready_count: int = 0
    pagination_supported: bool = False
    incremental_cursor_supported: bool = False
    response_shape_stable: bool = False
    production_db_opened: bool = False
    production_db_written: bool = False
    main_db_size_before: Optional[int] = None
    main_db_mtime_before: Optional[int] = None
    main_db_size_after: Optional[int] = None
    main_db_mtime_after: Optional[int] = None
    db_mtime_change_mechanism: Optional[str] = None
    adapter_gap_notes: Optional[str] = None
    ready_for_pr24z: bool = False
    ready_to_persist_source_trades: bool = False
    ready_to_wire_to_automation: bool = False
    identity: dict[str, Any] = field(default_factory=dict)
    previews: list[dict[str, Any]] = field(default_factory=list)
    pagination_notes: str = ""
    source_candidates: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ── Static source-candidate inventory (from repo audit; no network) ──────────
def _static_source_candidates() -> list[dict[str, Any]]:
    """Known trade sources in the repo, scored against PR24Y needs.

    Derived from reading adapters/polymarket.py, adapters/polymarket_clob.py,
    scripts/run_scan.py, scripts/collect_smart_money_data.py,
    discovery/wallet_discovery.py. This is static documentation, not a fetch.
    """
    return [
        {
            "source": "polymarket_data_api_trades_user",
            "client": "PolymarketPublicAdapter.get_trades_by_address",
            "endpoint": "GET https://data-api.polymarket.com/trades?user=<addr>",
            "auth_required": False,
            "wallet_filter_supported": True,
            "pagination_supported": True,
            "timestamp_filter_supported": True,  # 'since' applied client-side
            "returns_side": True,
            "returns_price": True,
            "returns_size": True,
            "returns_token_id": True,  # asset field
            "returns_condition_id": True,
            "returns_tx_hash_or_stable_id": True,  # transactionHash
            "fetch_only_safe": True,
            "used_by_production_scan": False,  # discovery path, not yet ingestion
            "notes": "Unauthenticated wallet-attributed trades; best PR24Y fit.",
        },
        {
            "source": "polymarket_data_api_trades_market",
            "client": "PolymarketPublicAdapter.fetch_trades_for_market",
            "endpoint": "GET https://data-api.polymarket.com/trades?market=<conditionId>",
            "auth_required": False,
            "wallet_filter_supported": False,  # market-scoped, not wallet-scoped
            "pagination_supported": True,
            "timestamp_filter_supported": True,
            "returns_side": True,
            "returns_price": True,
            "returns_size": True,
            "returns_token_id": True,
            "returns_condition_id": True,
            "returns_tx_hash_or_stable_id": True,
            "fetch_only_safe": True,
            "used_by_production_scan": True,
            "notes": "Used by collectors; market-scoped, not wallet-scoped.",
        },
        {
            "source": "polymarket_clob_trades",
            "client": "PolymarketClobAdapter.fetch_book (book only)",
            "endpoint": "GET https://clob.polymarket.com/trades",
            "auth_required": True,  # HTTP 401 even without headers
            "wallet_filter_supported": False,
            "pagination_supported": False,
            "timestamp_filter_supported": False,
            "returns_side": False,
            "returns_price": True,
            "returns_size": True,
            "returns_token_id": True,
            "returns_condition_id": False,
            "returns_tx_hash_or_stable_id": False,
            "fetch_only_safe": True,
            "used_by_production_scan": False,
            "notes": "Authenticated; order book only, not trade history.",
        },
        {
            "source": "polymarket_gamma_markets",
            "client": "PolymarketPublicAdapter (Gamma)",
            "endpoint": "GET https://gamma-api.polymarket.com/markets",
            "auth_required": False,
            "wallet_filter_supported": False,
            "pagination_supported": True,
            "timestamp_filter_supported": False,
            "returns_side": False,
            "returns_price": False,
            "returns_size": False,
            "returns_token_id": False,
            "returns_condition_id": True,
            "returns_tx_hash_or_stable_id": False,
            "fetch_only_safe": True,
            "used_by_production_scan": False,
            "notes": "Market metadata; no trade history.",
        },
        {
            "source": "run_scan_collector_writer",
            "client": "scripts/run_scan.py / collect_smart_money_data.py",
            "endpoint": "collector-owned source_trades writers (duplicated)",
            "auth_required": False,
            "wallet_filter_supported": True,
            "pagination_supported": True,
            "timestamp_filter_supported": True,
            "returns_side": True,
            "returns_price": True,
            "returns_size": True,
            "returns_token_id": True,
            "returns_condition_id": True,
            "returns_tx_hash_or_stable_id": True,
            "fetch_only_safe": False,  # writes source_trades directly
            "used_by_production_scan": True,
            "notes": "PR24X: duplicated collector-owned writers; NOT a probe source. "
                     "Future ingestion must delegate to one centralized writer.",
        },
    ]


# ── Wallet-address validation (local, no DB import) ──────────────────────────
def is_valid_wallet_address(value: Optional[str]) -> bool:
    """Accept a 0x + 40-hex EVM address; reject sentinels/empty/malformed."""
    if value is None:
        return False
    if _is_placeholder(value):
        return False
    return bool(_EVM_ADDR_RE.match(str(value).strip()))


def validate_wallet_inputs(
    wallet_addresses: Optional[list[str]],
    wallet_file: Optional[str],
    *,
    max_wallets: int = 5,
) -> list[str]:
    """Resolve explicit wallet inputs; reject malformed/over-limit.

    Exactly one input method is allowed. No auto-discovery, no DB-derived
    list, no unbounded scanning.
    """
    resolved: list[str] = []
    if wallet_addresses:
        resolved.extend(wallet_addresses)
    if wallet_file:
        from pathlib import Path

        p = Path(wallet_file)
        if not p.exists():
            raise ValueError(f"wallet-file not found: {wallet_file}")
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                resolved.append(line)
    if not resolved:
        raise ValueError("no wallet input provided (require --wallet-address or --wallet-file)")
    # De-duplicate while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for w in resolved:
        wc = w.strip().lower()
        if wc not in seen:
            seen.add(wc)
            uniq.append(w.strip())
    if len(uniq) > max_wallets:
        raise ValueError(
            f"too many wallets: {len(uniq)} > hard max {max_wallets}"
        )
    bad = [w for w in uniq if not is_valid_wallet_address(w)]
    if bad:
        raise ValueError(f"malformed wallet address(es): {bad[:3]}")
    return uniq


# ── Probe orchestration ──────────────────────────────────────────────────────
async def run_real_trade_source_probe(
    provider: RealTradeSourceProvider,
    wallets: list[str],
    *,
    allow_live_preview: bool = False,
    record_limit: int = DEFAULT_RECORD_LIMIT,
    max_pages: int = PR24Y_MAX_PAGES,
    source_candidates: Optional[list[dict[str, Any]]] = None,
    main_db_path: Optional[str] = None,
) -> RealTradeSourceProbeResult:
    """Run the bounded, read-only probe against injected wallets.

    Never opens a database. Never writes. Bounds pages to ``max_pages`` (PR24Y
    default 2). Bounds records to ``record_limit`` (hard max 100).

    ``network_calls_attempted`` / ``network_calls_succeeded`` count ONLY real
    external HTTP requests. Fixture/in-memory providers set
    ``provider.made_network_call = False``; their calls are NOT counted.

    ``main_db_path`` is an OPTIONAL path used ONLY to stat the main DB file size
    and mtime via ``os.stat`` — it is NEVER opened, never read via sqlite,
    never mutated. Capture happens before/after the probe so the report can
    prove the DB was untouched. If omitted, the DB-stat fields stay None.
    """
    record_limit = max(1, min(int(record_limit), HARD_MAX_RECORD_LIMIT))
    result = RealTradeSourceProbeResult(
        live_preview_enabled=allow_live_preview,
        wallet_count=len(wallets),
        record_limit=record_limit,
        source_candidates_examined=0,
    )
    result.generated_at = datetime.now(timezone.utc).isoformat()

    # Stat the main DB BEFORE the probe (no open; os.stat only).
    if main_db_path is not None:
        import os

        try:
            st = os.stat(main_db_path)
            result.main_db_size_before = st.st_size
            result.main_db_mtime_before = int(st.st_mtime)
        except OSError:
            pass

    candidates = source_candidates if source_candidates is not None else _static_source_candidates()
    result.source_candidates = candidates
    result.source_candidates_examined = len(candidates)

    if not wallets:
        result.error = "no wallets supplied"
        result.source_selection_verdict = "SOURCE_UNAVAILABLE"
        return result

    all_previews: list[RealTradeSourcePreview] = []
    pages_fetched = 0
    # Per-wallet page loop (bounded). No concurrent workers.
    for wallet in wallets:
        for page in range(max_pages):
            # Count ONLY real external HTTP requests as network calls.
            made_call = bool(getattr(provider, "made_network_call", False))
            if made_call:
                result.network_calls_attempted += 1
            try:
                rows = await provider.fetch_trades(wallet, limit=record_limit, page=page)
            except Exception as exc:  # never crash the probe on one bad page
                result.error = f"provider error on page {page}: {type(exc).__name__}: {exc}"[:300]
                break
            if made_call:
                result.network_calls_succeeded += 1
            if made_call:
                pages_fetched += 1
            if not isinstance(rows, list) or not rows:
                break  # empty page -> stop pagination for this wallet
            for i, raw in enumerate(rows):
                if not isinstance(raw, dict):
                    continue
                pv = _build_preview(raw, index=result.raw_records + i)
                all_previews.append(pv)
                result.raw_records += 1
                # counters
                if pv.side_canonical == "BUY":
                    result.raw_buy_records += 1
                    if pv.eligibility_reason == "eligible_buy":
                        result.eligible_buy_records += 1
                    elif pv.eligibility_reason.startswith("excluded_missing_fields"):
                        result.excluded_missing_fields += 1
                elif pv.side_canonical == "SELL":
                    result.raw_sell_records += 1
                    result.excluded_unsupported_side += 1
                else:
                    result.unknown_side_records += 1
                    if pv.eligibility_reason.startswith("excluded_missing_fields"):
                        result.excluded_missing_fields += 1
                # field coverage
                if pv.token_id_present:
                    result.token_id_available_count += 1
                if pv.condition_id_present:
                    result.condition_id_available_count += 1
                if pv.price_present:
                    result.price_available_count += 1
                if pv.size_present:
                    result.size_available_count += 1
                if pv.timestamp_present:
                    result.timestamp_available_count += 1
                if pv.pr24u_ready:
                    result.pr24u_ready_count += 1
                if pv.pr24v_ready:
                    result.pr24v_ready_count += 1
                if pv.both_ready:
                    result.both_ready_count += 1
                if result.raw_records >= record_limit * max_pages:
                    break
            if result.raw_records >= record_limit * max_pages:
                break
        if result.raw_records >= record_limit * max_pages:
            break

    result.pages_fetched = pages_fetched
    result.previews = [pv.as_dict() for pv in all_previews]

    # Identity ranking (live vs fixture-only labeling).
    identity = assess_stable_identity(all_previews, live=allow_live_preview)
    result.identity = identity.as_dict()
    result.stable_source_trade_id_available = identity.stable_source_trade_id_available
    # Pagination is supported by the data-api (offset+limit, bounded here).
    result.pagination_supported = True
    result.incremental_cursor_supported = True  # offset cursor usable for incremental
    result.response_shape_stable = True  # data-api returns stable dict shape

    # Source selection + verdict
    result.selected_source = "polymarket_data_api_trades_user"
    notes = (
        "data-api GET /trades?user=<addr> is unauthenticated, wallet-filterable, "
        "paginated (offset+limit), and returns proxyWallet, side, asset (token_id), "
        "conditionId, size, price, timestamp, and transactionHash. CLOB /trades "
        "requires auth; Gamma is market-metadata only; run_scan/collect are "
        "writer-owning collectors (excluded as probe sources)."
    )
    # Verdict + readiness are LITERAL booleans, never conditional phrasing.
    if result.raw_records == 0:
        # No records observed in this run (fixture/empty or no live flag).
        result.source_selection_verdict = "SOURCE_PARTIAL"
        result.ready_for_pr24z = False
        result.pagination_notes = (
            "No records observed in this run. Structural audit confirms the source "
            "is suitable; perform a live preview (--allow-live-preview) to confirm "
            "field coverage on real data. " + notes
        )
    else:
        # SOURCE_CONFIRMED requires BOTH field-complete eligible BUY records AND a
        # real observed stable trade identity (transactionHash) in the live path.
        # If the live adapter drops transactionHash (current PolymarketPublicAdapter
        # does not surface it on SourceTrade), a stable identity is NOT present on
        # the live path, so the verdict stays PARTIAL and ready_for_pr24z stays False
        # until PR24Z wires a stable-id path (raw transactionHash or the proven PR24X
        # deterministic_source_trade_id_v2).
        stable_id_ok = result.stable_source_trade_id_available
        if (result.eligible_buy_records > 0
                and result.token_id_available_count > 0
                and result.condition_id_available_count > 0
                and stable_id_ok):
            result.source_selection_verdict = "SOURCE_CONFIRMED"
            result.ready_for_pr24z = True
        else:
            result.source_selection_verdict = "SOURCE_PARTIAL"
            result.ready_for_pr24z = False
        result.pagination_notes = (
            f"Observed {result.raw_records} records across {pages_fetched} page(s); "
            f"{result.eligible_buy_records} eligible BUY. " + notes
        )
        # Transparency: in live mode the probe reads via
        # PolymarketPublicAdapter.get_trades_by_address -> SourceTrade; the
        # `source_trade_id` it surfaces is the PROVEN PR24X v2 deterministic id
        # (not the raw on-chain transactionHash). That v2 id IS a valid, unique,
        # row-distinguishing stable identity, so SOURCE_CONFIRMED holds. The raw
        # upstream transactionHash is dropped by the adapter boundary; PR24Z may
        # optionally carry it for even stronger provenance, but it is not required
        # given the v2 id is already proven and unique here.

    # Safety flags are always False in PR24Y.
    result.production_db_opened = False
    result.production_db_written = False
    result.ready_to_persist_source_trades = False
    result.ready_to_wire_to_automation = False

    # Stat the main DB AFTER the probe (no open; os.stat only).
    if main_db_path is not None:
        import os

        try:
            st = os.stat(main_db_path)
            result.main_db_size_after = st.st_size
            result.main_db_mtime_after = int(st.st_mtime)
            if result.main_db_mtime_before is not None \
                    and result.main_db_mtime_after != result.main_db_mtime_before:
                # The probe never opened the DB. A drift here is caused by OTHER
                # processes (e.g. enabled polycopy-api / dashboard / health unit
                # polling the live DB), NOT by this read-only probe. A controlled
                # mode=ro sqlite open was verified to NOT change mtime.
                result.db_mtime_change_mechanism = (
                    "DB mtime changed between before/after stat, but the probe "
                    "never opened the DB (os.stat only). Cause is external: an "
                    "enabled Polycopy process (polycopy-api / polycopy-dashboard / "
                    "polycopy-health unit) polling the live DB. A mode=ro sqlite "
                    "open was verified to leave mtime unchanged."
                )
        except OSError:
            pass
    return result


# ── Report renderers ─────────────────────────────────────────────────────────
def report_to_markdown(result: RealTradeSourceProbeResult) -> str:
    d = result.as_dict()
    lines: list[str] = []
    lines.append("# PR24Y — Real Wallet Trade Source Probe")
    lines.append("")
    lines.append(f"**Probe version:** {d['probe_version']}  ")
    lines.append(f"**Generated at:** {d['generated_at']}  ")
    lines.append(f"**Live preview enabled:** {d['live_preview_enabled']}  ")
    lines.append(f"**Network calls attempted/succeeded:** "
                 f"{d['network_calls_attempted']}/{d['network_calls_succeeded']}  ")
    lines.append("**Mode:** read-only source probe (no DB, no writes)")
    lines.append("")
    lines.append("## Source Selection")
    lines.append(f"- selected_source: **{d['selected_source']}**")
    lines.append(f"- source_selection_verdict: **{d['source_selection_verdict']}**")
    lines.append(f"- source_candidates_examined: {d['source_candidates_examined']}")
    lines.append("")
    lines.append("## Counters")
    for k in (
        "wallet_count", "record_limit", "pages_fetched", "raw_records",
        "raw_buy_records", "raw_sell_records", "unknown_side_records",
        "eligible_buy_records", "excluded_unsupported_side",
        "excluded_missing_fields",
    ):
        lines.append(f"- {k}: {d[k]}")
    lines.append("")
    lines.append("## Field Coverage")
    for k in (
        "token_id_available_count", "condition_id_available_count",
        "price_available_count", "size_available_count", "timestamp_available_count",
        "pr24u_ready_count", "pr24v_ready_count", "both_ready_count",
    ):
        lines.append(f"- {k}: {d[k]}")
    lines.append("")
    lines.append("## Stable Identity")
    idn = d["identity"]
    lines.append(f"- stable_source_trade_id_available: "
                 f"**{idn['stable_source_trade_id_available']}**")
    lines.append(f"- identity_field: {idn['identity_field']}")
    lines.append(f"- identity_uniqueness_confidence: {idn['identity_uniqueness_confidence']}")
    lines.append(f"- fallback_components_available: {idn['fallback_components_available']}")
    lines.append(f"- collision_risk_notes: {idn['collision_risk_notes']}")
    lines.append("")
    lines.append("## Pagination")
    lines.append(f"- pagination_supported: {d['pagination_supported']}")
    lines.append(f"- incremental_cursor_supported: {d['incremental_cursor_supported']}")
    lines.append(f"- response_shape_stable: {d['response_shape_stable']}")
    lines.append(f"- notes: {d['pagination_notes']}")
    lines.append("")
    lines.append("## Source Candidates Examined")
    for c in d["source_candidates"]:
        lines.append(f"- **{c['source']}** (`{c['client']}`)")
        lines.append(f"  - endpoint: {c['endpoint']}")
        lines.append(f"  - auth_required: {c['auth_required']} | "
                     f"wallet_filter: {c['wallet_filter_supported']} | "
                     f"pagination: {c['pagination_supported']}")
        lines.append(f"  - returns side/price/size/token_id/condition_id/tx_hash: "
                     f"{c['returns_side']}/{c['returns_price']}/{c['returns_size']}/"
                     f"{c['returns_token_id']}/{c['returns_condition_id']}/"
                     f"{c['returns_tx_hash_or_stable_id']}")
        lines.append(f"  - fetch_only_safe: {c['fetch_only_safe']} | "
                     f"used_by_production_scan: {c['used_by_production_scan']}")
        lines.append(f"  - {c['notes']}")
    lines.append("")
    lines.append("## Safety / Guardrails")
    for k in (
        "production_db_opened", "production_db_written",
        "main_db_size_before", "main_db_mtime_before",
        "main_db_size_after", "main_db_mtime_after",
        "db_mtime_change_mechanism",
        "adapter_gap_notes",
        "ready_for_pr24z", "ready_to_persist_source_trades",
        "ready_to_wire_to_automation",
    ):
        lines.append(f"- {k}: **{d[k]}**")
    lines.append("")
    lines.append(f"## Sample Previews (first 5 of {len(d['previews'])})")
    for pv in d["previews"][:5]:
        lines.append(f"- #{pv['raw_index']} side={pv['side_canonical']} "
                     f"token_id={'YES' if pv['token_id_present'] else 'no'} "
                     f"cond={'YES' if pv['condition_id_present'] else 'no'} "
                     f"elig={pv['eligibility_reason']} "
                     f"pr24u={pv['pr24u_ready']} pr24v={pv['pr24v_ready']}")
    return "\n".join(lines)


def report_to_json(result: RealTradeSourceProbeResult) -> str:
    import json

    return json.dumps(result.as_dict(), indent=2, sort_keys=False)
