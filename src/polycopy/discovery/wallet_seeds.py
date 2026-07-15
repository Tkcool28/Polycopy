"""Wallet discovery seeds — channels A (market-first) and B (leaderboard).

Produces a de-duplicated, deterministic list of wallet addresses with full
provenance so the historical-reconciliation step can fetch each wallet's
history exactly once.

Channel A — Market-first
    For every eligible market (from the short-horizon universe), fetches
    a bounded page of public trades filtered by condition ID, retains
    BUY and SELL on behavior analysis grounds, extracts valid
    ``proxyWallet`` addresses, dedupes by ``transactionHash`` then by
    ``(wallet, conditionId, side, price, size, timestamp)``.

Channel B — Category leaderboards
    For every category, fetches up to four leaderboard pages:
        * WEEK + PNL
        * WEEK + VOL
        * MONTH + PNL
        * MONTH + VOL

Leaderboard rank is seed provenance ONLY. It must NEVER be conflated
with score evidence; the canonical scorer only consumes
reconciled wallet history, not rank.

Channel membership is recorded so downstream code can split a wallet's
seed provenance: a wallet that surfaced via leaderboard rank gets the
``leaderboard`` source tag but is otherwise scored identically to a
market-first-seeded wallet.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import (
    DiscoveryAdapter,
    LEADERBOARD_CATEGORIES,
    LEADERBOARD_ORDERS,
    LEADERBOARD_PERIODS,
    extract_wallet_address,
)
from polycopy.discovery.market_universe import (
    ELIGIBLE_BUCKETS,
    MarketClassification,
)

logger = logging.getLogger(__name__)

DEFAULT_LEADERBOARD_TOP = 25
DEFAULT_MAX_WALLETS = 100
MAX_HARD_LIMIT = 100
MIN_HARD_LIMIT = 1


SEED_MARKET_FIRST = "market_first"
SEED_LEADERBOARD = "leaderboard"


# Trade identity key — derived only from upstream-required fields so a
# wallet-side dedupe cannot skip rows that share a wallet but differ in
# anything we know is real. Used to dedupe per-market and cross-market.
def _trade_identity_key(raw: Mapping[str, Any]) -> str:
    tx_hash = str(raw.get("transactionHash") or "").strip().lower()
    asset = str(raw.get("asset") or "")
    cond = str(raw.get("conditionId") or "").strip().lower()
    side = str(raw.get("side") or "").strip().upper()
    ts_raw = raw.get("timestamp")
    try:
        ts = str(int(float(ts_raw))) if ts_raw is not None else ""
    except (TypeError, ValueError):
        ts = ""
    price = raw.get("price")
    size = raw.get("size")
    try:
        price_s = f"{float(price):.10f}" if price is not None else ""
    except (TypeError, ValueError):
        price_s = ""
    try:
        size_s = f"{float(size):.10f}" if size is not None else ""
    except (TypeError, ValueError):
        size_s = ""
    payload = "|".join(["trade-id-v1", tx_hash, asset, cond, side, ts, price_s, size_s])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SeedWallet:
    """One wallet surfaced by discovery, with provenance."""

    wallet_address: str
    sources: tuple[str, ...]
    market_count: int = 0  # number of distinct markets whose trades surfaced this wallet
    leaderboard_count: int = 0
    leaderboard_records: tuple[dict[str, Any], ...] = ()
    first_trade_seen: str | None = None
    last_trade_seen: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SeedReport:
    """Audit + result of one seed-discovery run."""

    market_first_wallets: tuple[str, ...] = ()
    leaderboard_wallets: tuple[str, ...] = ()
    union_wallets: tuple[str, ...] = ()
    duplicate_wallets: tuple[str, ...] = ()
    leaderboard_top: int = DEFAULT_LEADERBOARD_TOP
    max_wallets: int = DEFAULT_MAX_WALLETS
    truncated: bool = False
    dropped_count: int = 0
    api_errors: tuple[tuple[str, str], ...] = ()
    markets_with_trades: int = 0
    trades_considered: int = 0
    invalid_wallet_rows: int = 0
    empty_markets: int = 0
    requested_categories: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["api_errors"] = [list(e) for e in self.api_errors]
        return out


def _category_to_leaderboard_enum(category_label: str) -> str | None:
    """Convert a taxonomy label (``sports``) to data-api enum (``SPORTS``)."""
    upper = (category_label or "").strip().upper()
    if upper in LEADERBOARD_CATEGORIES:
        return upper
    aliases = {
        "ESPORTS": "ESPORTS",
        "E-SPORTS": "ESPORTS",
        "CRYPTO": "CRYPTO",
        "CRYPTOCURRENCY": "CRYPTO",
    }
    return aliases.get(upper)


class WalletSeedBuilder:
    """Build wallet seed lists using both channels."""

    def __init__(
        self,
        adapter: DiscoveryAdapter,
        *,
        budget: _RequestBudget,
        leaderboard_top: int = DEFAULT_LEADERBOARD_TOP,
        max_wallets: int = DEFAULT_MAX_WALLETS,
        concurrency: int = 1,
    ) -> None:
        if not (MIN_HARD_LIMIT <= leaderboard_top <= MAX_HARD_LIMIT):
            raise ValueError(f"leaderboard_top must be in [{MIN_HARD_LIMIT}, {MAX_HARD_LIMIT}]")
        if not (MIN_HARD_LIMIT <= max_wallets <= MAX_HARD_LIMIT):
            raise ValueError(f"max_wallets must be in [{MIN_HARD_LIMIT}, {MAX_HARD_LIMIT}]")
        if not (1 <= int(concurrency) <= 4):
            raise ValueError("concurrency must be in [1, 4]")
        self._adapter = adapter
        self._budget = budget
        self._leaderboard_top = int(leaderboard_top)
        self._max_wallets = int(max_wallets)
        self._concurrency = int(concurrency)

    async def build(
        self,
        *,
        classifications: Iterable[MarketClassification],
        categories: Iterable[str],
    ) -> SeedReport:
        classifications = list(classifications)
        category_list = list(categories)
        # Channel A — market-first.
        channel_a_pairs: dict[str, set[str]] = defaultdict(set)  # wallet -> set(conditions)
        market_first_wallets: set[str] = set()
        api_errors: list[tuple[str, str]] = []
        trades_considered = 0
        invalid_wallet_rows = 0
        empty_markets = 0
        markets_with_trades = 0

        eligible = [c for c in classifications if c.bucket in ELIGIBLE_BUCKETS]

        # Sequential per-budget Channel A — never concurrent so the budget
        # cannot be violated by scheduling races.
        for cls in eligible:
            if self._budget.remaining <= 0:
                api_errors.append((cls.condition_id, "REQUEST_BUDGET_EXHAUSTED"))
                break
            trades, errors = await self._adapter.market_trades(
                condition_id=cls.condition_id,
                limit=100,
                offset=0,
                max_pages=1,
                budget=self._budget,
            )
            if trades:
                markets_with_trades += 1
            else:
                empty_markets += 1
            for err in errors:
                api_errors.append((cls.condition_id, str(err.get("error_code", "ERR"))))
            seen_ids: set[str] = set()
            for raw in trades:
                trades_considered += 1
                identity = _trade_identity_key(raw)
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
                wallet = extract_wallet_address(raw)
                if wallet is None:
                    invalid_wallet_rows += 1
                    continue
                market_first_wallets.add(wallet)
                channel_a_pairs[wallet].add(cls.condition_id)

        # Channel B — leaderboard per category x period x order.
        leaderboard_wallets: set[str] = set()
        leaderboard_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
        leaderboard_cats: list[str] = []
        for category in category_list:
            enum = _category_to_leaderboard_enum(category)
            if enum is None or enum == "OVERALL":
                continue
            leaderboard_cats.append(enum)
            for period in LEADERBOARD_PERIODS:
                if period in ("DAY", "ALL"):
                    continue
                for order_by in LEADERBOARD_ORDERS:
                    if self._budget.remaining <= 0:
                        api_errors.append((f"{enum}/{period}/{order_by}", "REQUEST_BUDGET_EXHAUSTED"))
                        continue
                    rows = await self._adapter.get_public_leaderboard(
                        category=enum,
                        time_period=period,
                        order_by=order_by,
                        limit=self._leaderboard_top,
                        budget=self._budget,
                    )
                    for row in rows:
                        wallet = extract_wallet_address(row)
                        if wallet is None:
                            invalid_wallet_rows += 1
                            continue
                        leaderboard_wallets.add(wallet)
                        leaderboard_records[wallet].append({
                            "category": enum,
                            "period": period,
                            "order_by": order_by,
                            "rank": row.get("rank"),
                            "pnl": row.get("pnl"),
                            "volume": row.get("vol") or row.get("volume") or row.get("volumeNum"),
                            "raw_keys": sorted(row.keys()),
                        })

        # Union + dedupe (Channel A ∩ B is recorded for audit).
        union_all = market_first_wallets | leaderboard_wallets
        duplicate_wallets = sorted(market_first_wallets & leaderboard_wallets)
        all_wallets_sorted = sorted(union_all)
        truncated = False
        dropped_count = 0
        if len(all_wallets_sorted) > self._max_wallets:
            # Deterministic truncation: keep the first N.
            kept = set(all_wallets_sorted[: self._max_wallets])
            dropped_count = len(union_all) - len(kept)
            all_wallets_sorted = [w for w in all_wallets_sorted if w in kept]
            truncated = True
        # Filter the source-set pairwise against truncation.
        market_first_filtered = [w for w in sorted(market_first_wallets) if w in set(all_wallets_sorted)]
        leaderboard_filtered = [w for w in sorted(leaderboard_wallets) if w in set(all_wallets_sorted)]

        # Build per-wallet SeedWallet records.
        kept_set = set(all_wallets_sorted)
        seed_wallets_list: list[SeedWallet] = []
        for wallet in all_wallets_sorted:
            in_a = wallet in market_first_wallets
            in_b = wallet in leaderboard_wallets
            sources: list[str] = []
            if in_a:
                sources.append(SEED_MARKET_FIRST)
            if in_b:
                sources.append(SEED_LEADERBOARD)
            seed_wallets_list.append(SeedWallet(
                wallet_address=wallet,
                sources=tuple(sorted(sources)),
                market_count=len(channel_a_pairs.get(wallet, set())),
                leaderboard_count=len(leaderboard_records.get(wallet, [])),
                leaderboard_records=tuple(leaderboard_records.get(wallet, [])),
                first_trade_seen=None,
                last_trade_seen=None,
            ))
        # Reference kept_set so pyright does not flag it as unused in some
        # toolchains; the audit report fields already encode the same info.
        _ = kept_set

        return SeedReport(
            market_first_wallets=tuple(market_first_filtered),
            leaderboard_wallets=tuple(leaderboard_filtered),
            union_wallets=tuple(all_wallets_sorted),
            duplicate_wallets=tuple(duplicate_wallets),
            leaderboard_top=self._leaderboard_top,
            max_wallets=self._max_wallets,
            truncated=truncated,
            dropped_count=dropped_count,
            api_errors=tuple(api_errors),
            markets_with_trades=markets_with_trades,
            trades_considered=trades_considered,
            invalid_wallet_rows=invalid_wallet_rows,
            empty_markets=empty_markets,
            requested_categories=tuple(leaderboard_cats),
        )


def rank_seed_wallets(
    seeds: Sequence[SeedWallet],
    *,
    channel_a_market_first: Iterable[str] = (),
    channel_b_leaderboard: Iterable[str] = (),
) -> list[SeedWallet]:
    """Deterministic evidence-priority ranking (STEP 14).

    NOT alphabetical address ordering. Rank by, in order:
      1. wallets found through BOTH channels (market-first ∩ leaderboard);
      2. greater number of distinct eligible market appearances;
      3. greater number of leaderboard appearances;
      4. better (smaller) best leaderboard rank;
      5. more supported categories (broader coverage);
      6. normalized address as final tie-breaker.

    ``market_count`` and ``leaderboard_count`` come from the SeedWallet
    provenance built by :meth:`WalletSeedBuilder.build`. Leaderboard rank
    remains seed provenance only and must NOT enter the frozen score.
    """
    a_set = {w.lower() for w in channel_a_market_first}
    b_set = {w.lower() for w in channel_b_leaderboard}

    def best_rank(rec: SeedWallet) -> int:
        ranks = []
        for r in rec.leaderboard_records:
            rk = r.get("rank")
            if isinstance(rk, int):
                ranks.append(rk)
            elif isinstance(rk, str) and rk.strip().isdigit():
                ranks.append(int(rk))
        return min(ranks) if ranks else 10 ** 9

    def supported_categories(rec: SeedWallet) -> int:
        return len({str(r.get("category") or "").lower() for r in rec.leaderboard_records if r.get("category")})

    def sort_key(rec: SeedWallet) -> tuple:
        addr = rec.wallet_address.lower()
        both = addr in a_set and addr in b_set
        return (
            0 if both else 1,                       # both channels first
            -rec.market_count,                       # more market appearances
            -rec.leaderboard_count,                  # more leaderboard appearances
            best_rank(rec),                          # better (smaller) rank
            -supported_categories(rec),              # broader category coverage
            addr,                                    # stable tie-breaker
        )

    return sorted(seeds, key=sort_key)


def seed_wallets_from_report(
    seeds: SeedReport,
    markets: Iterable[MarketClassification],
    trades: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[SeedWallet, ...]:
    """Convenience helper for offline tests: build SeedWallet records with
    real first/last timestamps from a fixture's trades mapping.

    Production flow uses :class:`WalletSeedBuilder` directly. This helper
    is the deterministic test path.
    """
    classifications = list(markets)
    eligible = [c for c in classifications if c.bucket in ELIGIBLE_BUCKETS]
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    markets_per_wallet: dict[str, set[str]] = defaultdict(set)

    for cls in eligible:
        rows = trades.get(cls.condition_id, [])
        for raw in rows:
            wallet = extract_wallet_address(raw)
            if wallet is None:
                continue
            markets_per_wallet[wallet].add(cls.condition_id)
            ts = str(raw.get("timestamp") or "")
            if not ts:
                continue
            current_first = first_seen.get(wallet)
            current_last = last_seen.get(wallet)
            if current_first is None or ts < current_first:
                first_seen[wallet] = ts
            if current_last is None or ts > current_last:
                last_seen[wallet] = ts

    out: list[SeedWallet] = []
    for wallet in seeds.union_wallets:
        in_a = wallet in seeds.market_first_wallets
        in_b = wallet in seeds.leaderboard_wallets
        if not in_a and not in_b:
            continue
        sources_list = []
        if in_a:
            sources_list.append(SEED_MARKET_FIRST)
        if in_b:
            sources_list.append(SEED_LEADERBOARD)
        out.append(SeedWallet(
            wallet_address=wallet,
            sources=tuple(sources_list),
            market_count=len(markets_per_wallet.get(wallet, set())),
            leaderboard_count=0,
            leaderboard_records=(),
            first_trade_seen=first_seen.get(wallet),
            last_trade_seen=last_seen.get(wallet),
        ))
    return tuple(out)


__all__ = [
    "DEFAULT_LEADERBOARD_TOP",
    "DEFAULT_MAX_WALLETS",
    "MAX_HARD_LIMIT",
    "MIN_HARD_LIMIT",
    "SEED_LEADERBOARD",
    "SEED_MARKET_FIRST",
    "SeedReport",
    "SeedWallet",
    "WalletSeedBuilder",
    "rank_seed_wallets",
    "seed_wallets_from_report",
]
