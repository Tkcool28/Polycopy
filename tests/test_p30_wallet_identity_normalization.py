"""Regression tests for canonical wallet-identity normalization.

Codex P2 finding (round 8): wallets discovered from the data-api can come
in mixed-case / padded forms (e.g. ``"0xAbCdEf..."`` with EIP-55
checksum, or ``" 0xReal "`` with surrounding whitespace). Legacy code
paths preserved the input byte-for-byte, which meant:

  - ``WalletDiscovery._register`` lowercased keys but ``source_trades``
    held the raw mixed-case form, so metric queries (exact-match
    ``= ?``) found zero trades and reported ``missing_data``.
  - ``_compute_wallet_metrics`` would not find a legacy mixed-case
    stored address when the freshly discovered wallet was lowercase.

The fix is end-to-end canonicalization:
  1. Parser (``_parse_data_api_trade``) lowercases real wallet addresses
     and trims surrounding whitespace; sentinel values normalize to None.
  2. Persistence (``_persist_trade``) defensively normalizes the
     attributed address to lowercase.
  3. ``_compute_wallet_metrics`` queries with
     ``LOWER(TRIM(trader_address)) = ?`` and a pre-lowercased parameter
     (``AND trader_address IS NOT NULL``), so a freshly-discovered
     lowercase wallet can find legacy mixed-case rows.
  4. ``deterministic_source_trade_id_v2`` already lowercases the wallet
     before hashing, so the deterministic ID is stable across case
     variants.
  5. Anonymous trades persist with ``NULL`` ``trader_address`` and never
     produce wallet rows.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket import (  # noqa: E402
    PolymarketPublicAdapter,
    deterministic_source_trade_id_v2,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.discovery.wallet_discovery import WalletDiscovery  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.domain.order import OrderSide  # noqa: E402
from polycopy.domain.source_trade import (  # noqa: E402
    SourceTrade,
)

import scripts.run_scan as run_scan_module  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _adapter() -> PolymarketPublicAdapter:
    return PolymarketPublicAdapter(
        gamma_base_url="https://gamma.example.test",
        clob_base_url="https://clob.example.test",
        data_api_base_url="https://data-api.example.test",
        data_api_request_interval_seconds=0.0,
    )


def _raw_with_wallet(wallet) -> dict:
    # Use a current timestamp so run_scan's staleness filter does not drop the
    # trade before the end-to-end pipeline can score it.
    return {
        "proxyWallet": wallet,
        "side": "BUY",
        "asset": "asset-yes",
        "conditionId": "0xMARKET_A",
        "size": 7.0,
        "price": 0.51,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "outcome": "Yes",
        "transactionHash": "0x123456789abcdef0",
    }


def _market(source_id: str = "0xMARKET_A") -> Market:
    return Market(
        source_id=source_id,
        question="Test market",
        outcomes=[MarketOutcome(label="Yes", price=0.7, volume=20_000.0)],
        source="polymarket",
        active=True,
        closed=False,
        resolved=False,
        volume_24h=20_000.0,
        fetched_at=datetime.now(timezone.utc),
        is_sample=False,
    )


def _trade(
    market_source_id: str,
    source_trade_id: str,
    trader_address: str | None,
) -> SourceTrade:
    return SourceTrade(
        source="polymarket_data_api",
        source_trade_id=source_trade_id,
        market_source_id=market_source_id,
        side=OrderSide.BUY,
        outcome="Yes",
        quantity=10.0,
        price=0.45,
        trader_address=trader_address,
        timestamp=datetime.now(timezone.utc),
        is_sample=False,
    )


def _db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "p30.sqlite").connect()


# ─── 1. Parser normalization (cases 1–4) ──────────────────────────────────────


class TestParserWalletIdentityNormalization:
    """The parser must lower-case real wallet addresses and turn sentinels
    into None — at the parser boundary."""

    def test_mixed_case_proxy_wallet_parses_to_lowercase(self):
        # EIP-55 checksum-style address — typical data-api output.
        wallet = "0xAbCdEf1234567890aBcDeF1234567890AbCdEf12"
        trade = _adapter()._parse_data_api_trade(_raw_with_wallet(wallet))  # noqa: SLF001
        assert trade is not None
        assert trade.trader_address == wallet.lower()

    def test_already_lowercase_wallet_stays_unchanged(self):
        wallet = "0xabcdef0000000000000000000000000000000001"
        trade = _adapter()._parse_data_api_trade(_raw_with_wallet(wallet))  # noqa: SLF001
        assert trade is not None
        assert trade.trader_address == wallet

    def test_surrounding_whitespace_is_trimmed_before_lowercasing(self):
        wallet = "  0xAbCdEf0000000000000000000000000000000001 \t\n"
        trade = _adapter()._parse_data_api_trade(_raw_with_wallet(wallet))  # noqa: SLF001
        assert trade is not None
        assert trade.trader_address == "0xabcdef0000000000000000000000000000000001"

    @pytest.mark.parametrize(
        "wallet",
        [
            None,
            "",
            "   ",
            "\t\n",
            "unknown",
            " Unknown ",
            "UNKNOWN",
            "anonymous",
            " Anonymous ",
            "missing",
            " MiSsInG ",
            "0x",
            " 0X ",
            "0x0",
            " 0X0 ",
        ],
    )
    def test_sentinel_values_become_none(self, wallet):
        trade = _adapter()._parse_data_api_trade(_raw_with_wallet(wallet))  # noqa: SLF001
        assert trade is not None
        assert trade.trader_address is None


# ─── 2. Persistence normalization (case 5) ────────────────────────────────────


class TestPersistenceWalletIdentityNormalization:
    def test_persisted_source_trades_trader_address_is_lowercase(self, tmp_path: Path):
        """``_persist_trade`` must store canonical lowercase trader_address
        even if a non-parser caller passes a mixed-case value."""
        db = _db(tmp_path)
        try:
            # Bypass parser — direct SourceTrade with mixed-case + padding.
            trade = _trade(
                "0xMARKET_A",
                "p30-mixed-case",
                "  0xAbCdEf0000000000000000000000000000000099  ",
            )
            result = run_scan_module._persist_trade(db, trade)  # noqa: SLF001
            assert result is True

            rows = db.fetchall(
                "SELECT source_trade_id, trader_address FROM source_trades"
            )
            assert [dict(r) for r in rows] == [
                {
                    "source_trade_id": "p30-mixed-case",
                    "trader_address": "0xabcdef0000000000000000000000000000000099",
                }
            ]
        finally:
            db.close()


# ─── 3. WalletDiscovery canonical key (case 6) ────────────────────────────────


class TestWalletDiscoveryCanonicalKey:
    def test_discovery_uses_lowercase_key_for_mixed_case_input(self):
        """``_register`` lowercases the address — discovery, metrics, and
        the persistence path must all agree on the same canonical form."""
        discovery = WalletDiscovery()
        a = discovery.add_from_polymarket("0xAbCdEf000000000000000000000000000000ABCD")
        b = discovery.add_from_polymarket("0xabcdef000000000000000000000000000000abcd")
        c = discovery.add_from_polymarket("  0xABCDEF000000000000000000000000000000ABCD  ")
        # All three collapse onto one canonical entry.
        assert a["address"] == b["address"] == c["address"]
        assert a["address"] == "0xabcdef000000000000000000000000000000abcd"
        # Same set of sources means only one canonical row.
        assert len(discovery.list_wallets()) == 1


# ─── 4. _compute_wallet_metrics case-insensitive match (cases 7–9) ────────────


class TestComputeWalletMetricsCaseInsensitive:
    def test_compute_wallet_metrics_finds_legacy_mixed_case_row(
        self, tmp_path: Path
    ):
        """A legacy row persisted with mixed-case / checksum address must
        be findable by the freshly-discovered lowercase wallet."""
        db = _db(tmp_path)
        try:
            legacy = "0xAbCdEf0000000000000000000000000000000099"
            # Insert a legacy source_trade row with mixed-case (pre-round-8
            # storage) directly, bypassing _persist_trade's defensive
            # normalization.
            db.execute(
                "INSERT INTO source_trades (id, source, source_trade_id, "
                "market_source_id, side, outcome, quantity, price, "
                "trader_address, timestamp, is_sample) "
                "VALUES (?, 'polymarket_data_api', 'legacy-1', '0xMARKET_A', "
                "'buy', 'Yes', 10.0, 0.45, ?, ?, 0)",
                (str(uuid.uuid4()), legacy, datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()

            # Query with the lowercase canonical key.
            metrics = run_scan_module._compute_wallet_metrics(  # noqa: SLF001
                db, legacy.lower(), datetime.now(timezone.utc)
            )
            assert metrics is not None, "expected metrics for legacy mixed-case row"
            assert metrics["trade_count"] == 1
        finally:
            db.close()

    def test_newly_fetched_mixed_case_wallet_is_scored(self, tmp_path: Path):
        """Calling ``_compute_wallet_metrics`` with mixed-case (newly
        fetched, just-discovered) input must still match a lowercase row
        (because the function lowercases the parameter)."""
        db = _db(tmp_path)
        try:
            db.execute(
                "INSERT INTO source_trades (id, source, source_trade_id, "
                "market_source_id, side, outcome, quantity, price, "
                "trader_address, timestamp, is_sample) "
                "VALUES (?, 'polymarket_data_api', 'p30-lower', '0xMARKET_A', "
                "'buy', 'Yes', 5.0, 0.4, ?, ?, 0)",
                (
                    str(uuid.uuid4()),
                    "0xabcdef0000000000000000000000000000000011",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.conn.commit()

            # Caller passes mixed-case (newly fetched, not yet normalized).
            metrics = run_scan_module._compute_wallet_metrics(  # noqa: SLF001
                db, "0xAbCdEf0000000000000000000000000000000011",
                datetime.now(timezone.utc),
            )
            assert metrics is not None
            assert metrics["trade_count"] == 1
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_no_false_missing_data_for_legacy_mixed_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A legacy mixed-case row plus a freshly-discovered lowercase
        wallet must NOT produce a ``missing_data`` entry in the scan
        result. Pre-fix this was the symptom reported to Codex."""
        db = _db(tmp_path)
        try:
            legacy = "0xAbCdEf00000000000000000000000000000000AA"
            db.execute(
                "INSERT INTO source_trades (id, source, source_trade_id, "
                "market_source_id, side, outcome, quantity, price, "
                "trader_address, timestamp, is_sample) "
                "VALUES (?, 'polymarket_data_api', 'p30-legacy', '0xMARKET_A', "
                "'buy', 'Yes', 3.0, 0.5, ?, ?, 0)",
                (str(uuid.uuid4()), legacy, datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()

            # Pre-seed a wallets row for the lowercase canonical key so
            # run_scan loads it.
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, ?, 'p30', 0, ?)",
                (
                    str(uuid.uuid4()),
                    legacy.lower(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.conn.commit()

            # Stub fetchers so run_scan doesn't hit the network.
            async def fake_fetch_markets(db, settings, limit, result, use_sample):
                return [], {}

            async def fake_fetch_trades(
                db, market_source_id, now, result, use_sample,
                *, asset_to_outcome=None,
            ):
                from polycopy.adapters.polymarket import MarketTradeFetchResult
                return MarketTradeFetchResult(
                    trades=[],
                    status="complete",
                    pages_fetched=0,
                    rows_fetched=0,
                    market_source_id=market_source_id,
                )

            def fake_generate_signals(db, markets, now):
                return []

            monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
            monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
            monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)

            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result.missing_data == [], (
                f"unexpected missing_data entries: {result.missing_data}"
            )
            # The legacy row was loaded as the lowercase wallet and
            # scored (no error path triggered).
            assert result.errors == [], f"unexpected errors: {result.errors}"
        finally:
            db.close()


# ─── 5. Idempotency (case 10) ────────────────────────────────────────────────


class TestWalletIdentityIdempotency:
    def test_exact_rerun_remains_idempotent(self, tmp_path: Path):
        """Re-running the scan with identical inputs must produce the
        same number of persisted rows (UNIQUE source_trade_id index)."""
        db = _db(tmp_path)
        try:
            trade = _trade(
                "0xMARKET_A",
                "p30-idem",
                "  0xAbCdEf00000000000000000000000000000000BB  ",
            )
            first = run_scan_module._persist_trade(db, trade)  # noqa: SLF001
            assert first is True
            second = run_scan_module._persist_trade(db, trade)  # noqa: SLF001
            # Idempotent retry — the second call hits UNIQUE(source, source_trade_id).
            assert second is False
            rows = db.fetchall("SELECT source_trade_id FROM source_trades")
            assert [r["source_trade_id"] for r in rows] == ["p30-idem"]
        finally:
            db.close()


# ─── 6. Deterministic source-trade ID (case 11) ──────────────────────────────


class TestDeterministicSourceTradeId:
    def test_deterministic_id_identical_across_wallet_case_variants(self):
        """``deterministic_source_trade_id_v2`` must produce the same ID
        for the same trade with mixed-case vs lowercase ``proxyWallet``,
        because it lowercases the wallet before hashing."""
        base = {
            "side": "BUY",
            "asset": "asset-yes",
            "conditionId": "0xMARKET_A",
            "size": 7.0,
            "price": 0.51,
            "timestamp": 1_782_636_254,
            "outcome": "Yes",
            "transactionHash": "0x123456789abcdef0",
        }
        mixed = dict(base, proxyWallet="0xAbCdEf00000000000000000000000000000000CC")
        lower = dict(base, proxyWallet="0xabcdef00000000000000000000000000000000cc")
        upper = dict(base, proxyWallet="0xABCDEF00000000000000000000000000000000CC")
        padded = dict(
            base, proxyWallet="  0xAbCdEf00000000000000000000000000000000CC  "
        )

        ids = {
            deterministic_source_trade_id_v2(mixed),
            deterministic_source_trade_id_v2(lower),
            deterministic_source_trade_id_v2(upper),
            deterministic_source_trade_id_v2(padded),
        }
        assert len(ids) == 1, f"expected 1 deterministic ID, got {ids}"


# ─── 7. Anonymous trade behavior (case 12) ───────────────────────────────────


class TestAnonymousTradeBehavior:
    def test_anonymous_trade_persists_with_null_and_no_wallet(
        self, tmp_path: Path
    ):
        """An anonymous trade (``trader_address=None`` or a sentinel)
        persists with NULL ``trader_address`` and never creates a wallet
        row."""
        db = _db(tmp_path)
        try:
            # 1. Explicit None
            t1 = _trade("0xMARKET_A", "p30-anon-1", None)
            # 2. Sentinel string (would be coerced to None by parser; here
            #    the persistence layer also defends)
            t2 = _trade("0xMARKET_A", "p30-anon-2", "unknown")

            assert run_scan_module._persist_trade(db, t1) is True  # noqa: SLF001
            assert run_scan_module._persist_trade(db, t2) is True  # noqa: SLF001

            rows = db.fetchall(
                "SELECT source_trade_id, trader_address FROM source_trades "
                "ORDER BY source_trade_id"
            )
            assert [tuple(r) for r in rows] == [
                ("p30-anon-1", None),
                ("p30-anon-2", None),
            ]
            # No wallet rows were created.
            wallet_count = db.fetchall("SELECT COUNT(*) AS n FROM wallets")
            assert wallet_count and wallet_count[0]["n"] == 0
        finally:
            db.close()


# ─── 8. End-to-end: parser → persistence → discovery → metrics ───────────────


class TestEndToEndMixedCase:
    @pytest.mark.asyncio
    async def test_end_to_end_parser_persistence_discovery_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Full pipeline: a data-api row with mixed-case proxyWallet
        must end up with a discoverable, scorable wallet, and the
        metrics must find the persisted trade.

        Uses a fresh DB and a single run_scan pass — no pre-persistence
        of the trade, since run_scan itself is the persistence path
        under test here. The pre-persistence checks above (parser,
        discovery, metrics) use a *separate* DB so this end-to-end
        run_scan starts from a clean state.
        """
        pre_db = _db(tmp_path)
        try:
            # Mixed-case wallet directly from the data-api.
            mixed_wallet = "0xAbCdEf0000000000000000000000000000000DD"
            canonical = mixed_wallet.lower()

            # Pre-parse via the parser to confirm it normalizes.
            parsed = _adapter()._parse_data_api_trade(  # noqa: SLF001
                _raw_with_wallet(mixed_wallet)
            )
            assert parsed is not None
            assert parsed.trader_address == canonical

            # Persist through run_scan's helper (separate DB so this
            # does not interfere with the run_scan end-to-end below).
            persisted = run_scan_module._persist_trade(pre_db, parsed)  # noqa: SLF001
            assert persisted is True

            # Discovery sees the canonical lowercase key.
            discovery = WalletDiscovery()
            assert parsed.trader_address is not None
            entry = discovery.add_from_polymarket(parsed.trader_address)
            assert entry["address"] == canonical

            # Metrics finds the persisted trade using a case-insensitive
            # match against the lowercase key.
            metrics = run_scan_module._compute_wallet_metrics(  # noqa: SLF001
                pre_db, canonical, datetime.now(timezone.utc)
            )
            assert metrics is not None
            assert metrics["trade_count"] == 1
        finally:
            pre_db.close()

        # ── End-to-end run_scan: fresh DB, single trade, one pass ────
        e2e_db = _db(tmp_path / "e2e.sqlite")
        try:
            e2e_market = _market(source_id="0xMARKET_B")
            e2e_wallet = "0xFeDcBa0000000000000000000000000000000EE"
            e2e_canonical = e2e_wallet.lower()

            async def fake_fetch_markets(db, settings, limit, result, use_sample):
                return [e2e_market], {}

            async def fake_fetch_trades(
                db, market_source_id, now, result, use_sample,
                *, asset_to_outcome=None,
            ):
                # Return parsed SourceTrade objects; run_scan passes them
                # straight into the persistence + discovery pipeline.
                from polycopy.adapters.polymarket import MarketTradeFetchResult
                parsed = [_adapter()._parse_data_api_trade(  # noqa: SLF001
                    _raw_with_wallet(e2e_wallet)
                )]
                return MarketTradeFetchResult(
                    trades=parsed,
                    status="complete",
                    pages_fetched=1,
                    rows_fetched=len(parsed),
                    market_source_id=market_source_id,
                )

            def fake_generate_signals(db, markets, now):
                return []

            monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
            monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
            monkeypatch.setattr(
                run_scan_module, "_generate_signals", fake_generate_signals
            )

            result = await run_scan_module.run_scan(  # noqa: SLF001
                e2e_db, market_limit=1, use_sample=False
            )
            assert result.errors == [], f"unexpected errors: {result.errors}"
            assert result.trades_persisted == 1
            assert result.trades_attributed == 1
            assert result.missing_data == [], (
                f"unexpected missing_data: {result.missing_data}"
            )
            assert result.wallets_scored == 1
            # The wallet must be stored with the canonical lowercase address.
            rows = e2e_db.fetchall(
                "SELECT address FROM wallets WHERE address = ?", (e2e_canonical,)
            )
            assert [r["address"] for r in rows] == [e2e_canonical]
        finally:
            e2e_db.close()
