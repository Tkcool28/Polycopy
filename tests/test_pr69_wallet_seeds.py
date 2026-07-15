"""Correction tests: wallet seed deterministic ranking + provenance (STEP 14)."""
from __future__ import annotations


from polycopy.discovery.wallet_seeds import (
    SeedWallet,
    rank_seed_wallets,
)


def _seed(
    addr: str,
    *,
    markets: int = 1,
    leaderboard: list[dict] | None = None,
    sources: tuple[str, ...] = ("market_first",),
) -> SeedWallet:
    records = tuple(leaderboard or [])
    return SeedWallet(
        wallet_address=addr,
        sources=sources,
        market_count=markets,
        leaderboard_count=len(records),
        leaderboard_records=records,
        first_trade_seen=None,
        last_trade_seen=None,
    )


def test_rank_preserves_provenance_channels():
    """STEP 14: a both-channel wallet keeps both canonical source labels."""
    s = _seed("0xA", markets=5, leaderboard=[{"rank": 3}], sources=("market_first", "leaderboard"))
    s2 = _seed("0xB", markets=5, leaderboard=[{"rank": 3}], sources=("market_first", "leaderboard"))
    ranked = rank_seed_wallets([s, s2])
    both = {r.wallet_address for r in ranked if len(r.sources) >= 2}
    assert "0xA" in both
    assert "0xB" in both
    # Canonical labels only — never formatted channel:addr strings.
    assert all(set(r.sources) <= {"market_first", "leaderboard"} for r in ranked)


def test_rank_not_alphabetical():
    """STEP 14: rank is evidence-priority, NOT alphabetical address order."""
    low = _seed("0xAAAA", markets=10, leaderboard=[{"rank": 1}])
    high = _seed("0xBBBB", markets=1)
    ranked = rank_seed_wallets([low, high])
    # The stronger evidence (more markets + rank 1) should rank first despite
    # lexicographically-later address.
    assert ranked[0].wallet_address == "0xAAAA"


def test_rank_both_channels_before_single():
    """Both channels outrank a single-channel seed (explicit both-channel sources)."""
    both = _seed(
        "0xBOTH",
        markets=2,
        leaderboard=[{"rank": 5}],
        sources=("market_first", "leaderboard"),
    )
    single = _seed("0xSINGLE", markets=9, leaderboard=[{"rank": 2}])
    ranked = rank_seed_wallets(
        [single, both],
        channel_a_market_first=("0xBOTH",),
        channel_b_leaderboard=("0xBOTH",),
    )
    assert ranked[0].wallet_address == "0xBOTH"


def test_rank_more_markets_beats_fewer():
    a = _seed("0xMORE", markets=20)
    b = _seed("0xLESS", markets=3)
    ranked = rank_seed_wallets([a, b])
    assert ranked[0].wallet_address == "0xMORE"


def test_rank_leaderboard_rank_used():
    a = _seed("0xR1", markets=1, leaderboard=[{"rank": 1}])
    b = _seed("0xR9", markets=1, leaderboard=[{"rank": 9}])
    ranked = rank_seed_wallets([a, b])
    assert ranked[0].wallet_address == "0xR1"


def test_rank_deterministic():
    seeds = [
        _seed("0xZ", markets=1),
        _seed("0xY", markets=5, leaderboard=[{"rank": 2}]),
        _seed("0xX", markets=5, leaderboard=[{"rank": 1}]),
    ]
    r1 = rank_seed_wallets(seeds)
    r2 = rank_seed_wallets(list(reversed(seeds)))
    assert [s.wallet_address for s in r1] == [s.wallet_address for s in r2]
