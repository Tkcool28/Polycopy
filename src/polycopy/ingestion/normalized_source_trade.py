"""PR24Z — Normalized in-memory source-trade candidate model.

This module is the pure normalization / validation / identity core of the
manual real source-trade ingestion slice. It contains NO database access and
NO network access — it maps raw data-api-shaped dicts (or already-parsed
``SourceTrade`` rows) into validated :class:`NormalizedSourceTrade` candidates.

The centralized writer (``source_trade_writer.py``) is the ONLY component that
may insert these into the production ``source_trades`` table.

BUY-only V1 rule (carried from PR24Y):
  * side canonicalized to UPPERCASE.
  * BUY eligible.
  * SELL rejected as ``unsupported_side``.
  * missing/unknown side rejected as ``missing_side``.

Stable identity (PR24X deterministic id, carried forward):
  * Strong identity: the upstream ``transactionHash`` (or an upstream stable
    fill id). Prefer ``transactionHash + deterministic record index`` when the
    same transaction can legitimately contain multiple fills.
  * Fallback identity: a deterministic composite key over
    (source, wallet, token_id/conditionId, side, price, quantity, timestamp,
    outcome) ONLY when the available fields provide a repeatable identity.
  * Ambiguous identity: when even the fallback cannot distinguish two rows
    (e.g. two fills share price+quantity+timestamp and no row-distinguishing
    field), the row is marked ``identity_ambiguous`` and reported/rejected
    rather than silently merged.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from polycopy.ingestion.source_trade_metadata import normalize_source_trade_metadata

# ── Constants ────────────────────────────────────────────────────────────────
INGESTION_VERSION = "PR24Z-1"
SOURCE_NAME = "polymarket_data_api_trades_user"

# Bounds (mirrors PR24Y network-safety envelope).
DEFAULT_RECORD_LIMIT = 25
HARD_MAX_RECORD_LIMIT = 100
HARD_MAX_PAGES = 2

# Validation / rejection reason codes.
REASON_BUY_ELIGIBLE = "buy_eligible"
REASON_UNSUPPORTED_SIDE = "unsupported_side"
REASON_MISSING_SIDE = "missing_side"
REASON_MISSING_FIELDS = "missing_fields"
REASON_INVALID_PRICE = "invalid_price"
REASON_INVALID_QUANTITY = "invalid_quantity"
REASON_INVALID_TIMESTAMP = "invalid_timestamp"
REASON_WALLET_MISMATCH = "wallet_mismatch"
REASON_PLACEHOLDER = "placeholder_present"

# Identity strategy codes.
IDENTITY_STRONG = "strong"          # upstream transactionHash (or stable fill id)
IDENTITY_FALLBACK = "fallback"      # deterministic composite key
IDENTITY_AMBIGUOUS = "ambiguous"    # cannot be proven unique -> reject/report

# Identity *source* kind codes (which strong source produced the id).
IDENTITY_SOURCE_PROVIDED = "source_provided_trade_id"   # Polymarket v2 stable id
IDENTITY_SOURCE_TX = "transaction_hash"                 # real on-chain tx hash
IDENTITY_SOURCE_FALLBACK = "fallback"                   # deterministic composite

# A real on-chain transaction hash: 0x + 8+ hex chars.
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{8,}$")
# A conditionId-shaped identifier: 0x + 64 hex chars (Keccak).
_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
# A CLOB token id: 0x + 64 hex chars OR a decimal numeric asset string.
_TOKEN_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID_DEC_RE = re.compile(r"^[0-9]{10,}$")
# EVM address: 0x + 40 hex chars.
_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Legacy sentinel / placeholder trader-address tokens (mirrors source_trade.py).
_SENTINELS = frozenset(
    {
        "unknown",
        "anonymous",
        "missing",
        "0x",
        "0x0",
        "0x0000000000000000000000000000000000000000",
    }
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _is_placeholder(value: Optional[str]) -> bool:
    """True if value is missing or a legacy sentinel token."""
    if value is None:
        return True
    s = str(value).strip().lower()
    if not s:
        return True
    return s in _SENTINELS


def _is_valid_token_id(value: Optional[str]) -> bool:
    """Accept 0x-hex (64) or decimal numeric asset string; reject placeholders."""
    if not isinstance(value, str):
        return False
    if _is_placeholder(value):
        return False
    return bool(_TOKEN_ID_RE.match(value) or _TOKEN_ID_DEC_RE.match(value))


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an int/float Unix seconds or ISO string into a UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _canonicalize_wallet(value: Optional[str]) -> Optional[str]:
    """Lowercase a real wallet address; return None for sentinels/empty."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or _is_placeholder(s):
        return None
    return s.lower()


# ── Normalized candidate model ─────────────────────────────────────────────────
@dataclass
class NormalizedSourceTrade:
    """A validated, normalized, in-memory candidate for source_trades.

    Produced by :func:`normalize_source_trade` from a raw data-api dict (or a
    parsed ``SourceTrade``). ``validation_status`` is ``valid`` only when the
    row passed every BUY-only gate. ``source_trade_id`` is a deterministic
    stable identity (strong or fallback); ``None`` when identity is ambiguous.
    """

    source: str = SOURCE_NAME
    source_trade_id: Optional[str] = None
    trader_address: Optional[str] = None
    market_source_id: str = ""
    token_id: Optional[str] = None
    side: str = ""                 # canonicalized UPPERCASE: BUY / SELL / ""
    price: Optional[float] = None
    quantity: Optional[float] = None
    timestamp: Optional[datetime] = None
    outcome: Optional[str] = None
    transaction_hash: Optional[str] = None
    market_title: Optional[str] = None
    market_slug: Optional[str] = None
    # Versioned source evidence; event.slug is identity/provenance, never a category.
    metadata: Optional[dict[str, Any]] = None

    # Provenance flags (always 0 for live records).
    is_sample: int = 0

    # Identity bookkeeping.
    identity_source: str = ""       # IDENTITY_SOURCE_* code
    identity_strong: bool = False
    identity_fallback: bool = False
    identity_ambiguous: bool = False

    # Distinct strong-identity counters.
    identity_source_provided: bool = False   # Polymarket v2 stable id (canonical)
    identity_transaction_hash: bool = False   # real on-chain tx hash + fill index

    # Optional raw provenance (kept separate; never fed through fallback).
    source_provided_trade_id: Optional[str] = None   # Polymarket v2 stable id
    transaction_hash: Optional[str] = None           # real on-chain hash (sep)

    # Validation.
    validation_status: str = "pending"   # "valid" | "rejected"
    validation_reasons: list[str] = field(default_factory=list)

    # Readiness (observational; no mapping performed here).
    pr24u_ready: bool = False
    pr24v_ready: bool = False
    both_ready: bool = False

    # Raw loop/page index (deterministic + repeatable) for strong-id record index.
    _fetch_index: int = field(default=-1, repr=False)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat() if self.timestamp else None
        return d


# ── Stable identity ────────────────────────────────────────────────────────────
@dataclass
class IdentityResult:
    """Outcome of identity generation for one normalized candidate."""

    source_trade_id: Optional[str]
    strategy: str                 # IDENTITY_STRONG | IDENTITY_FALLBACK | IDENTITY_AMBIGUOUS
    strong: bool = False
    fallback: bool = False
    ambiguous: bool = False
    notes: str = ""


def _namespace_v2_id(v2_id: str) -> str:
    """Namespace a Polymarket v2 stable id consistently as ``polymarket:<id>``.

    The adapter returns ``source_trade_id`` already prefixed with
    ``polymarket:``. We accept either form and always emit exactly
    ``polymarket:<id>`` so the identity is stable across adapter versions
    and matches rows previously persisted (the 14 PR24Z rows used exactly
    this form).
    """
    s = str(v2_id).strip().lower()
    if s.startswith("polymarket:"):
        return s
    return "polymarket:" + s


def _strong_identity(raw: dict[str, Any], *, record_index: int) -> tuple[Optional[str], str, bool]:
    """Strong identity resolution.

    Returns ``(source_trade_id, identity_source_kind, is_source_provided)``.

    Preference order (per PR24Z safety-correction):
      1. Source-provided stable trade/fill ID (``sourceProvidedTradeId`` /
         adapter ``source_trade_id`` / v2 id) -> canonical ``polymarket:<id>``.
         This is the PREFERRED strong identity; it is NOT routed through the
         fallback composite.
      2. Real on-chain transaction hash (+ deterministic record index when a
         single transaction legitimately carries multiple fills) ->
         ``polymarket:<tx>[:<index>]``.
      3. Not a strong identity -> ``(None, "", False)`` (caller falls back).

    The two raw fields are preserved SEPARATELY on the candidate (never
    relabeled into one another): ``source_provided_trade_id`` and
    ``transaction_hash``.
    """
    # 1. Source-provided stable trade/fill ID.
    sp = raw.get("sourceProvidedTradeId")
    if sp is None:
        sp = raw.get("source_trade_id")
    if isinstance(sp, str) and sp.strip():
        return _namespace_v2_id(sp), IDENTITY_SOURCE_PROVIDED, True

    # 2. Real on-chain transaction hash.
    tx = raw.get("transactionHash")
    if isinstance(tx, str) and _TX_HASH_RE.match(tx.strip()):
        tx = tx.strip().lower()
        if record_index is not None and record_index >= 0:
            return f"polymarket:{tx}:{record_index}", IDENTITY_SOURCE_TX, False
        return f"polymarket:{tx}", IDENTITY_SOURCE_TX, False

    return None, "", False


def _fallback_identity(raw: dict[str, Any]) -> Optional[str]:
    """Deterministic composite fallback key.

    Used ONLY when no strong identity is available. Requires enough fields to
    be repeatable. Returns None if the field set is too thin to be trusted
    (caller treats that as ambiguous).
    """
    wallet = _canonicalize_wallet(
        str(raw.get("proxyWallet") or raw.get("maker") or raw.get("trader") or "")
    )
    token = str(raw.get("asset") or "")
    cond = str(raw.get("conditionId") or "").strip().lower()
    side = str(raw.get("side") or "").strip().upper()
    outcome = str(raw.get("outcome") or "")
    price = _as_float(raw.get("price"))
    size = _as_float(raw.get("size"))
    ts = _parse_timestamp(raw.get("timestamp"))

    # Minimum required fields for a *trusted* deterministic fallback.
    if wallet is None or not token or not cond or not side or price is None \
            or size is None or ts is None:
        return None

    payload = "|".join([
        "fallback",
        wallet,
        token,
        cond,
        side,
        outcome,
        f"{price:.10f}",
        f"{size:.10f}",
        str(int(ts.timestamp())),
    ])
    return "polymarket:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generate_identity(
    raw: dict[str, Any], *, record_index: int = -1
) -> IdentityResult:
    """Compute a stable identity for a raw trade dict.

    Resolution order:
      1. source-provided stable trade/fill ID -> IDENTITY_STRONG
         (identity_source = source_provided_trade_id)
      2. real transaction hash + stable fill index -> IDENTITY_STRONG
         (identity_source = transaction_hash)
      3. deterministic fallback composite -> IDENTITY_FALLBACK
      4. otherwise -> IDENTITY_AMBIGUOUS (caller must report/reject)

    ``strong_identity_used_count`` = source_provided_identity_used_count
        + transaction_identity_used_count`` (enforced by the caller counter).
    """
    sid, kind, is_sp = _strong_identity(raw, record_index=record_index)
    if sid is not None:
        if is_sp:
            return IdentityResult(
                source_trade_id=sid,
                strategy=IDENTITY_STRONG,
                strong=True,
                notes="strong identity from source-provided stable trade/fill id",
            )
        return IdentityResult(
            source_trade_id=sid,
            strategy=IDENTITY_STRONG,
            strong=True,
            notes="strong identity from real transaction hash (+ fill index)",
        )
    fallback = _fallback_identity(raw)
    if fallback is not None:
        return IdentityResult(
            source_trade_id=fallback,
            strategy=IDENTITY_FALLBACK,
            fallback=True,
            notes="fallback deterministic composite identity (no strong source)",
        )
    return IdentityResult(
        source_trade_id=None,
        strategy=IDENTITY_AMBIGUOUS,
        ambiguous=True,
        notes=(
            "ambiguous identity: no source-provided id and no transaction hash, "
            "and insufficient fields for a trusted deterministic composite key"
        ),
    )


# ── Normalization + validation ──────────────────────────────────────────────────
def _side_uppercase(value: Any) -> Optional[str]:
    """Canonicalize side to UPPERCASE; None if missing/unknown."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("buy", "1"):
        return "BUY"
    if s in ("sell", "0"):
        return "SELL"
    return None


def normalize_source_trade(
    raw: dict[str, Any],
    *,
    requested_wallet: Optional[str] = None,
    record_index: int = -1,
    allow_sell: bool = False,
    gamma_market: Optional[Mapping[str, Any]] = None,
) -> "NormalizedSourceTrade":
    """Normalize one record; ``allow_sell`` is opt-in for bounded history.

    The recurring collector leaves ``allow_sell=False`` and therefore remains
    BUY-only. Historical evidence explicitly passes True.

    ``gamma_market`` (PR68): an OPTIONAL trusted Gamma market dict (the one the
    trade's ``conditionId`` resolves to). When provided, canonical metadata is
    built from it (event/series/category/tags) via the PR66 helper — the
    approved-wallet trade endpoint itself returns no taxonomy fields. No
    title/slug inference is ever performed; market ``title``/``slug`` are kept
    only as non-taxonomy identity/provenance. When omitted, metadata degrades
    to the honest all-null (UNAVAILABLE) shape.
    """

    # BUY-only V1 rules enforced by the default:
    #   * side canonicalized to UPPERCASE; SELL -> rejected unsupported_side;
    #     missing/unknown -> rejected missing_side.
    #   * price parseable within [0, 1]; quantity > 0; timestamp parseable.
    #   * trader_address must match requested_wallet (when one is supplied).
    #   * live rows: is_sample always 0.
    #   * no placeholder IDs (token_id/conditionId must be valid, not sentinel).
    candidate = NormalizedSourceTrade()
    candidate._fetch_index = record_index

    # ── side ──
    side = _side_uppercase(raw.get("side"))
    candidate.side = side or ""
    if side is None:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append(REASON_MISSING_SIDE)
    elif side == "SELL" and not allow_sell:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append(REASON_UNSUPPORTED_SIDE)

    # ── token_id / conditionId (reject placeholders) ──
    asset = raw.get("asset")
    if asset is None:
        asset = raw.get("token_id")  # DB-row style field name
    cond = raw.get("conditionId")
    if cond is None:
        cond = raw.get("market_source_id")  # DB-row style field name
    token_ok = _is_valid_token_id(asset)
    cond_ok = isinstance(cond, str) and _CONDITION_ID_RE.match(cond) is not None
    # Placeholder detection: present-but-sentinel OR present-but-malformed
    # token/conditionId values are a distinct rejection reason. An absent
    # field is covered by the missing-fields check below.
    placeholder_hit = False
    if asset is not None and not token_ok:
        placeholder_hit = True
    if cond is not None and not cond_ok:
        placeholder_hit = True
    if placeholder_hit:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append(REASON_PLACEHOLDER)
    candidate.token_id = str(asset) if (asset not in (None, "") and token_ok) else None
    candidate.market_source_id = str(cond).strip().lower() if cond_ok else ""

    # ── price ──
    price = _as_float(raw.get("price"))
    if price is not None:
        if 0.0 <= price <= 1.0:
            candidate.price = price
        else:
            candidate.price = None
            candidate.validation_status = "rejected"
            candidate.validation_reasons.append(REASON_INVALID_PRICE)
    else:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append(REASON_INVALID_PRICE)

    # ── quantity ──
    qty = _as_float(raw.get("size") if raw.get("size") is not None else raw.get("quantity"))
    if qty is None:
        qty = _as_float(raw.get("quantity"))  # DB-row style field name
    if qty is not None and qty > 0.0:
        candidate.quantity = qty
    else:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append(REASON_INVALID_QUANTITY)

    # ── timestamp ──
    ts = _parse_timestamp(raw.get("timestamp"))
    if ts is not None:
        candidate.timestamp = ts
    else:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append(REASON_INVALID_TIMESTAMP)

    # ── trader address ──
    wallet = _canonicalize_wallet(
        str(raw.get("proxyWallet") or raw.get("maker") or raw.get("trader")
            or raw.get("trader_address") or "")
    )
    candidate.trader_address = wallet
    if requested_wallet is not None:
        req = requested_wallet.strip().lower()
        if wallet is None or wallet != req:
            candidate.validation_status = "rejected"
            candidate.validation_reasons.append(REASON_WALLET_MISMATCH)

    # ── outcome / market metadata ──
    outcome = raw.get("outcome")
    candidate.outcome = str(outcome).strip() if isinstance(outcome, str) and outcome.strip() else None
    candidate.market_title = raw.get("title") if isinstance(raw.get("title"), str) else None
    candidate.market_slug = raw.get("slug") if isinstance(raw.get("slug"), str) else None
    candidate.metadata = normalize_source_trade_metadata(raw)
    # PR68: when a trusted Gamma market is supplied, rebuild canonical metadata
    # from it (the trade endpoint carries no event/taxonomy/series fields).
    if gamma_market is not None:
        from polycopy.ingestion.source_trade_metadata import (
            build_metadata_from_gamma_market,
        )

        candidate.metadata = build_metadata_from_gamma_market(raw, gamma_market)

    # ── raw provenance: keep source-provided id and tx hash SEPARATE ──
    sp = raw.get("sourceProvidedTradeId")
    if sp is None:
        sp = raw.get("source_trade_id")
    candidate.source_provided_trade_id = sp if isinstance(sp, str) and sp.strip() else None
    tx = raw.get("transactionHash")
    candidate.transaction_hash = tx if isinstance(tx, str) and tx.strip() else None

    # ── live rows: is_sample always 0 ──
    candidate.is_sample = 0

    # ── readiness (observational) ──
    candidate.pr24u_ready = candidate.token_id is not None
    candidate.pr24v_ready = bool(candidate.market_source_id)
    candidate.both_ready = candidate.pr24u_ready and candidate.pr24v_ready

    # ── identity ──
    ident = generate_identity(raw, record_index=record_index)
    candidate.source_trade_id = ident.source_trade_id
    kind = IDENTITY_SOURCE_PROVIDED if ident.notes.startswith("strong identity from source-provided") else (
        IDENTITY_SOURCE_TX if ident.strategy == IDENTITY_STRONG else (
            IDENTITY_SOURCE_FALLBACK if ident.strategy == IDENTITY_FALLBACK else ""))
    candidate.identity_source = kind
    candidate.identity_strong = ident.strong
    candidate.identity_fallback = ident.fallback
    candidate.identity_ambiguous = ident.ambiguous
    candidate.identity_source_provided = bool(ident.strong and kind == IDENTITY_SOURCE_PROVIDED)
    candidate.identity_transaction_hash = bool(ident.strong and kind == IDENTITY_SOURCE_TX)

    # Ambiguous identity -> reject (cannot be proven unique).
    if ident.ambiguous:
        candidate.validation_status = "rejected"
        candidate.validation_reasons.append("ambiguous_identity")

    # Missing required fields (for an otherwise BUY side) -> missing_fields.
    if candidate.validation_status != "rejected":
        missing = []
        if candidate.trader_address is None:
            missing.append("trader_address")
        if candidate.token_id is None:
            missing.append("token_id")
        if not candidate.market_source_id:
            missing.append("conditionId")
        if candidate.price is None:
            missing.append("price")
        if candidate.quantity is None:
            missing.append("quantity")
        if candidate.timestamp is None:
            missing.append("timestamp")
        if missing:
            candidate.validation_status = "rejected"
            candidate.validation_reasons.append(REASON_MISSING_FIELDS + ":" + ",".join(missing))
        else:
            candidate.validation_status = "valid"
            candidate.validation_reasons.append(REASON_BUY_ELIGIBLE)

    return candidate


# ── Batch orchestration helpers (pure; no DB, no network) ──────────────────────
@dataclass
class IngestionCounters:
    """All required PR24Z counters in one place."""

    # Fetch
    wallets_requested: int = 0
    pages_fetched: int = 0
    raw_records: int = 0
    # Classification
    raw_buy_records: int = 0
    raw_sell_records: int = 0
    unknown_side_records: int = 0
    eligible_buy_records: int = 0
    rejected_unsupported_side: int = 0
    rejected_missing_fields: int = 0
    rejected_invalid_price: int = 0
    rejected_invalid_quantity: int = 0
    rejected_invalid_timestamp: int = 0
    rejected_wallet_mismatch: int = 0
    rejected_invalid_fields: int = 0
    # Identity
    stable_ids_generated: int = 0
    source_provided_identity_used_count: int = 0
    transaction_identity_used_count: int = 0
    strong_identity_used_count: int = 0
    identity_fallback_used_count: int = 0
    identity_ambiguous_count: int = 0
    duplicate_records_in_fetch: int = 0
    duplicate_records_existing_db: int = 0
    collision_errors: int = 0
    # Write
    write_requested: int = 0
    production_db_opened: int = 0
    rows_attempted: int = 0
    rows_inserted: int = 0
    rows_deduplicated: int = 0
    rows_rejected: int = 0
    transaction_committed: int = 0
    transaction_rolled_back: int = 0
    # Readiness
    pr24u_ready_count: int = 0
    pr24v_ready_count: int = 0
    both_ready_count: int = 0
    # Safety
    downstream_tables_changed: int = 0
    timers_changed: int = 0
    ready_for_scoring: int = 0
    ready_for_automation: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def count_rejection(counters: "IngestionCounters", candidate: "NormalizedSourceTrade") -> None:
    """Map a rejected candidate reason onto the required counters."""
    for reason in candidate.validation_reasons:
        base = reason.split(":")[0]
        if base == REASON_UNSUPPORTED_SIDE:
            counters.rejected_unsupported_side += 1
        elif base == REASON_MISSING_SIDE:
            counters.rejected_missing_fields += 1
        elif base == REASON_MISSING_FIELDS:
            counters.rejected_missing_fields += 1
        elif base == REASON_INVALID_PRICE:
            counters.rejected_invalid_price += 1
        elif base == REASON_INVALID_QUANTITY:
            counters.rejected_invalid_quantity += 1
        elif base == REASON_INVALID_TIMESTAMP:
            counters.rejected_invalid_timestamp += 1
        elif base == REASON_WALLET_MISMATCH:
            counters.rejected_wallet_mismatch += 1
        elif base == REASON_PLACEHOLDER:
            counters.rejected_invalid_fields += 1
        elif base == "ambiguous_identity":
            counters.identity_ambiguous_count += 1
