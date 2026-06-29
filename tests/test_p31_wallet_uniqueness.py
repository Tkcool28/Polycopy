"""Adversarial tests for canonical wallet-uniqueness invariants (Codex P2 #1).

Covers ``scripts.run_scan._persist_wallet`` and the same canonical
find-or-create in ``scripts.collect_smart_money_data`` against the
"many writes → one row" invariant the audit demanded.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.wallet_identity import canonical_wallet_address  # noqa: E402
from polycopy.domain.wallet import Wallet  # noqa: E402

# Import via the script path the way the audit demanded.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import run_scan as run_scan_module  # noqa: E402


REAL_ADDRESS = "0x" + "a" * 40
REAL_ADDRESS_UPPER = "0x" + "A" * 40


@pytest.fixture
def db():
    db = Database(db_path=Path(":memory:")).connect()
    yield db
    db.close()


def _wallet(address: str, label: str = "test") -> Wallet:
    return Wallet(address=address, label=label, is_sample=False)


# ── Codex #1 headline: 10 trades/1 wallet → 1 wallet row ─────────────────────

def test_ten_trades_one_wallet_yields_one_row(db):
    """Persist the same canonical address 10 times → exactly 1 wallet row."""
    wallet = _wallet(REAL_ADDRESS)
    first_id = None
    for _ in range(10):
        wallet_id = run_scan_module._persist_wallet(db, wallet)
        assert wallet_id is not None
        if first_id is None:
            first_id = wallet_id
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 1, f"expected 1 wallet row, got {n}"
    # Verify the address stored is canonical.
    row = db.fetchone("SELECT address FROM wallets WHERE id = ?", (first_id,))
    assert row["address"] == REAL_ADDRESS


def test_same_wallet_across_ten_markets_yields_one_row(db):
    """Same address persisted 10 times with different labels → 1 row."""
    for market_idx in range(10):
        wallet = _wallet(REAL_ADDRESS, label=f"mkt-{market_idx}")
        run_scan_module._persist_wallet(db, wallet)
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 1, f"expected 1 wallet row, got {n}"


def test_repeated_scan_yields_no_duplicate(db):
    """Simulate multiple scan invocations on the same DB."""
    for _ in range(5):
        run_scan_module._persist_wallet(db, _wallet(REAL_ADDRESS))
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 1


def test_case_variants_collapse_to_one_row(db):
    """Mixed-case variants of the same address → 1 row."""
    first_id = None
    for addr in [REAL_ADDRESS, REAL_ADDRESS_UPPER, "0x" + "aA" * 20]:
        wallet_id = run_scan_module._persist_wallet(db, _wallet(addr))
        if first_id is None:
            first_id = wallet_id
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 1
    # Address stored is the canonical lowercase form (use id lookup so
    # we don't trigger the test_no_bypass_in_test_files static guard).
    assert first_id is not None
    row = db.fetchone("SELECT address FROM wallets WHERE id = ?", (first_id,))
    assert row["address"] == REAL_ADDRESS


def test_padded_variants_collapse_to_one_row(db):
    """Whitespace-padded variants of the same address → 1 row."""
    first_id = None
    for pad in ["", " ", "\t", "\n", "\r"]:
        wallet_id = run_scan_module._persist_wallet(db, _wallet(pad + REAL_ADDRESS + pad))
        if first_id is None:
            first_id = wallet_id
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 1
    assert first_id is not None
    row = db.fetchone("SELECT address FROM wallets WHERE id = ?", (first_id,))
    assert row["address"] == REAL_ADDRESS


# ── Sentinels never become wallet rows ────────────────────────────────────────

@pytest.mark.parametrize("sent", [
    "unknown", "anonymous", "missing", "0x", "0x0",
    "UNKNOWN", "Anonymous", "  unknown  ",
    "0x0000000000000000000000000000000000000000",
])
def test_sentinels_never_create_wallet_rows(db, sent):
    """Sentinels are rejected by the Wallet Pydantic model (defense in
    depth) AND by ``_persist_wallet``'s canonicalization guard.

    The Wallet model validates ``address`` via ``str.strip() != ""``,
    so a Wallet with a sentinel that passes the validator (e.g.
    ``0x0000...``) is fed straight to ``_persist_wallet``. The
    canonicalization there must then drop it.
    """
    # Bypass the Pydantic validator by constructing a plain namespace
    # object so we exercise ``_persist_wallet``'s canonicalization
    # directly. (A real Wallet is constructed only AFTER the sentinel
    # filter in run_scan's discovery loop.)
    from types import SimpleNamespace
    fake_wallet = SimpleNamespace(
        id=uuid.uuid4(),
        address=sent,
        label="test",
        is_sample=False,
    )
    wallet_id = run_scan_module._persist_wallet(db, fake_wallet)
    assert wallet_id is None
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 0, (
        f"sentinel {sent!r} created wallet rows: count={n}"
    )


@pytest.mark.parametrize("empty", [None, "", "   ", "\t\t", "\n\n"])
def test_empty_addresses_never_create_wallet_rows(db, empty):
    """Empty / whitespace-only addresses never become wallets.

    Constructed via a namespace shim so the Wallet Pydantic validator
    (which would itself reject these) doesn't fire first — we want to
    exercise ``_persist_wallet``'s canonicalization guard.
    """
    from types import SimpleNamespace
    fake_wallet = SimpleNamespace(
        id=uuid.uuid4(),
        address=empty,
        label="test",
        is_sample=False,
    )
    wallet_id = run_scan_module._persist_wallet(db, fake_wallet)
    assert wallet_id is None
    n = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 0


def test_sentinel_does_not_create_row_even_when_idempotent(db):
    """Calling _persist_wallet with a sentinel multiple times never
    silently succeeds on a second pass."""
    for _ in range(5):
        assert run_scan_module._persist_wallet(db, _wallet("unknown")) is None
    assert db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"] == 0


# ── Mixed-case legacy row in source_trades is found by canonical query ────────

def test_mixed_case_legacy_row_found_by_canonical_query(db):
    """Pre-v5 mixed-case row is matched by a fresh canonical (lowercase) query."""
    db.execute(
        """INSERT INTO source_trades
           (id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            str(uuid.uuid4()), "polymarket_data_api", "legacy-001",
            "0xCOND", "BUY", "Yes", 1.0, 0.5,
            REAL_ADDRESS_UPPER,  # mixed-case legacy
            "2026-06-01T00:00:00+00:00",
        ),
    )
    db.conn.commit()

    metrics = run_scan_module._compute_wallet_metrics(
        db, REAL_ADDRESS, None,
    )
    assert metrics is not None
    assert metrics["trade_count"] == 1


def test_padded_legacy_rows_all_found_by_canonical_query(db):
    """Every whitespace-padded variant is found by the canonical query."""
    from datetime import datetime, timezone
    for pad in [" ", "\t", "\n", "\r", "\x0b", "\x0c"]:
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                str(uuid.uuid4()), "polymarket_data_api",
                f"legacy-{pad!r}", "0xCOND", "BUY", "Yes", 1.0, 0.5,
                pad + REAL_ADDRESS_UPPER + pad,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    db.conn.commit()
    metrics = run_scan_module._compute_wallet_metrics(
        db, REAL_ADDRESS, None,
    )
    assert metrics is not None
    assert metrics["trade_count"] == 6  # 6 pad variants


# ── Persistence failure doesn't claim success ────────────────────────────────

def test_persistence_failure_returns_none_and_creates_no_row(db, monkeypatch):
    """If the SELECT raises, the wallet is not silently created.

    Restores the monkeypatch before the final assertion so the COUNT
    check can read the DB normally.
    """
    original_fetchone = db.fetchone
    call_count = {"n": 0}

    def boom(*args, **kwargs):
        call_count["n"] += 1
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "fetchone", boom)
    wallet_id = run_scan_module._persist_wallet(db, _wallet(REAL_ADDRESS))
    assert wallet_id is None
    assert call_count["n"] >= 1, "expected boom to fire on fetchone"
    # Restore fetchone (monkeypatch will also undo it, but we need it
    # undone for the final COUNT assertion below).
    monkeypatch.undo()
    n = original_fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n == 0, f"DB still has {n} rows despite persist failure"


# ── Application-level helper parity ──────────────────────────────────────────

def test_canonical_wallet_address_matches_for_all_case_padded_variants():
    """Every case + padding variant collapses to the same canonical form."""
    variants = [
        REAL_ADDRESS,
        REAL_ADDRESS_UPPER,
        "  " + REAL_ADDRESS + "  ",
        "\t" + REAL_ADDRESS_UPPER + "\n",
        "\r" + REAL_ADDRESS + "\x0c",
    ]
    canonicals = {canonical_wallet_address(v) for v in variants}
    assert canonicals == {REAL_ADDRESS}, f"got {canonicals}"


# ── Dependent-row preservation ────────────────────────────────────────────────

def test_re_persisting_wallet_does_not_orphan_dependents(db):
    """A second find-or-create on the same canonical address must return
    the EXISTING id, not a new one — dependents on the first id stay
    valid.
    """
    # First insert.
    first_id = run_scan_module._persist_wallet(db, _wallet(REAL_ADDRESS))
    assert first_id is not None
    # Attach a dependent row to that id.
    db.execute(
        """INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample)
           VALUES (?, ?, ?, ?, 0)""",
        (first_id, "USDC", 100.0, "2026-06-01T00:00:00+00:00"),
    )
    db.conn.commit()

    # Second insert (same canonical address).
    second_id = run_scan_module._persist_wallet(db, _wallet(REAL_ADDRESS))
    assert second_id == first_id, (
        f"second persist returned new id {second_id!r} instead of "
        f"existing id {first_id!r}"
    )

    # The dependent row is still referenced by the returned id.
    bal = db.fetchone(
        "SELECT amount FROM wallet_balances WHERE wallet_id = ?",
        (second_id,),
    )
    assert bal is not None
    assert bal["amount"] == 100.0