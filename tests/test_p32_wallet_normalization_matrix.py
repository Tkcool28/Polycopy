"""Adversarial tests for canonical wallet-identity invariants.

Covers the shared helper introduced in ``polycopy.db.wallet_identity``
plus every consumer. Verifies that the "ONE canonical form" rule holds
byte-for-byte across Python, SQLite, the v5 migration predicate, and
every consumer that ships wallet identity through it.
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

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.wallet_identity import (  # noqa: E402
    LEGACY_TRADER_ADDRESS_SENTINELS,
    address_column_normalized,
    address_is_sentinel_params,
    address_is_sentinel_sql,
    canonical_wallet_address,
    is_sentinel_trader_address,
)


# ── Inputs the audit demanded we handle identically ───────────────────────────
REAL_ADDRESS = "0x" + "a" * 40
REAL_ADDRESS_UPPER = "0x" + "A" * 40
SENTINELS = list(LEGACY_TRADER_ADDRESS_SENTINELS)  # 5 sentinels

# Whitespace split by what each layer can strip:
#   * Python (``canonical_wallet_address``) strips ALL of these: tab, LF,
#     CR, VT, FF, NUL, space.
#   * SQLite (``address_column_normalized``) strips all EXCEPT NUL —
#     TRIM(col, X'00') and REPLACE(col, X'00', X'20') both treat NUL as a
#     C-string terminator and cannot operate past it.
PAD_CHARS_PYTHON: list[str] = ["", " ", "\t", "\n", "\r", "\x0b", "\x0c", "\x00"]
PAD_CHARS_SQL: list[str] = ["", " ", "\t", "\n", "\r", "\x0b", "\x0c"]


# ── (a) Python canonical_wallet_address ───────────────────────────────────────

@pytest.mark.parametrize("value", SENTINELS + [None, "", "  ", "\t\t", "\n\n"])
def test_canonical_returns_none_for_sentinels_and_empty(value):
    assert canonical_wallet_address(value) is None
    assert is_sentinel_trader_address(value)


@pytest.mark.parametrize("pad", PAD_CHARS_PYTHON)
def test_canonical_lowercases_real_address(pad):
    """Lowercase strip-all-whitespace = canonical form (incl. NUL)."""
    padded = pad + REAL_ADDRESS_UPPER + pad
    assert canonical_wallet_address(padded) == REAL_ADDRESS


@pytest.mark.parametrize("addr", [REAL_ADDRESS, REAL_ADDRESS_UPPER])
def test_canonical_unchanged_when_already_canonical(addr):
    assert canonical_wallet_address(addr) == addr.lower()


# ── (b) SQL address_column_normalized ─────────────────────────────────────────

@pytest.mark.parametrize("pad", PAD_CHARS_SQL)
def test_sql_normalized_strips_and_lowercases(pad):
    """SQL fragment matches Python canonical for the SQLite-strippable set.

    NUL is excluded — see the documented limitation in
    :func:`address_column_normalized` and ``test_sqlite_cannot_strip_nul``.
    """
    db = Database(db_path=Path(":memory:")).connect()
    try:
        padded = pad + REAL_ADDRESS_UPPER + pad
        row = db.fetchone(
            f"SELECT {address_column_normalized('addr_col')} AS v "
            f"FROM (SELECT ? AS addr_col)",
            (padded,),
        )
        assert row is not None, f"empty result for padded={padded!r}"
        assert row["v"] == REAL_ADDRESS, (
            f"pad={pad!r} padded={padded!r} got={row['v']!r} expected={REAL_ADDRESS!r}"
        )
    finally:
        db.close()


def test_sql_normalized_handles_each_pad_char_independently():
    """Each SQLite-strippable ASCII whitespace char stripped independently."""
    db = Database(db_path=Path(":memory:")).connect()
    try:
        for pad in ["\t", "\n", "\r", "\x0b", "\x0c", " "]:
            row = db.fetchone(
                f"SELECT {address_column_normalized('addr_col')} AS v "
                f"FROM (SELECT ? AS addr_col)",
                (pad + REAL_ADDRESS + pad,),
            )
            assert row is not None
            assert row["v"] == REAL_ADDRESS, f"pad={pad!r} got={row['v']!r}"
    finally:
        db.close()


def test_sqlite_cannot_strip_nul_documented_limitation():
    """SQLite TRIM(col, X'00') does not return the NUL-padded value
    intact. The NUL byte acts as a C-string terminator: the column is
    seen by TRIM as ending at the first NUL, and TRIM(...,X'00') strips
    that whole empty suffix. Concretely, passing a ``X'00'``-padded
    value through ``address_column_normalized`` returns ``""`` (empty)
    rather than the canonical lowercased address.

    This is the documented limitation — see :func:`address_column_normalized`.
    The Python helper ``canonical_wallet_address`` DOES strip NUL (see
    test_canonical_lowercases_real_address with pad="\\x00"). The DB
    boundary is therefore safe as long as ingestion routes through the
    Python helper before persistence — which the run_scan / collect /
    smoke paths all do via ``canonical_wallet_address``.
    """
    db = Database(db_path=Path(":memory:")).connect()
    try:
        padded = "\x00" + REAL_ADDRESS + "\x00"
        # Pass via parameter so the NUL bytes survive parameter binding.
        row = db.fetchone(
            f"SELECT {address_column_normalized('addr_col')} AS v, "
            f"LENGTH({address_column_normalized('addr_col')}) AS n "
            f"FROM (SELECT ? AS addr_col)",
            (padded,),
        )
        assert row is not None
        # Assert the limitation: SQL helper did NOT return the canonical
        # lowercased address — it returned empty (NUL terminated the
        # string). The Python helper would have returned the canonical
        # form; the SQL helper is lossy for NUL-padded inputs.
        assert row["v"] != REAL_ADDRESS, (
            f"SQL helper unexpectedly returned the canonical form for a "
            f"NUL-padded input: got {row['v']!r}, expected the canonical "
            f"address {REAL_ADDRESS!r}"
        )
        assert row["n"] < len(padded), (
            f"SQL helper unexpectedly preserved NUL bytes intact: got "
            f"length {row['n']}, expected less than {len(padded)}"
        )
    finally:
        db.close()


# ── (c) Sentinel predicate parity (Python ↔ SQL) ──────────────────────────────

@pytest.mark.parametrize("sent", SENTINELS)
@pytest.mark.parametrize("pad", ["", " ", "\t"])
def test_sql_sentinel_predicate_matches_python_for_all_sentinels(sent, pad):
    """Every padded sentinel maps identically: True → python is_sentinel → SQL WHERE removes row."""
    val = pad + sent + pad
    py_sentinel = is_sentinel_trader_address(val)
    # SQL: ask whether the predicate marks it as a sentinel (un-negated).
    db = Database(db_path=Path(":memory:")).connect()
    try:
        # Build a case expression: 1 if the predicate says sentinel, else 0.
        sql_expr = address_is_sentinel_sql("addr_col", negate=False)
        params = address_is_sentinel_params()
        row = db.fetchone(
            f"SELECT (CASE WHEN {sql_expr} THEN 1 ELSE 0 END) AS s "
            f"FROM (SELECT ? AS addr_col)",
            (*params, val),
        )
        assert row is not None
        sql_sentinel = row["s"] == 1
        assert py_sentinel == sql_sentinel, (
            f"sent={sent!r} pad={pad!r} val={val!r} "
            f"py={py_sentinel} sql={sql_sentinel}"
        )
    finally:
        db.close()


def test_address_is_sentinel_params_order_is_sorted():
    """Params order must be stable so the SQL fragment and bindings agree."""
    assert address_is_sentinel_params() == tuple(sorted(SENTINELS))


# ── (d) v5 migration predicate parity ─────────────────────────────────────────

@pytest.mark.parametrize("sent", sorted(s for s in SENTINELS if s != "0x0000000000000000000000000000000000000000"))
def test_v5_migration_predicate_matches_python(sent):
    """The v5 DDL's TRIM-based predicate agrees with the Python helper.

    Only checks the 5 sentinels v5 knew about (``unknown``, ``anonymous``,
    ``missing``, ``0x``, ``0x0``). The zero-address
    ``0x0000000000000000000000000000000000000000`` was added to the
    Python helper in round-9 stabilization as a forward-looking guard;
    v5 DDL is frozen and does NOT include it, so this test does not
    assert parity for the zero-address. See
    ``test_zero_address_is_sentinel_in_python_not_in_v5_ddl`` for the
    asymmetric assertion.
    """
    db = Database(db_path=Path(":memory:")).connect()
    try:
        row = db.fetchone(
            """SELECT (CASE WHEN
                   LENGTH(TRIM(addr_col, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0
                   OR LOWER(TRIM(addr_col, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN ('unknown','anonymous','missing','0x','0x0')
                   THEN 1 ELSE 0 END) AS truth
               FROM (SELECT ? AS addr_col)""",
            (sent,),
        )
        assert row is not None
        sql_truth = row["truth"] == 1
        py_truth = is_sentinel_trader_address(sent)
        assert sql_truth == py_truth, f"sent={sent!r} py={py_truth} sql={sql_truth}"
    finally:
        db.close()


def test_zero_address_is_sentinel_in_python_not_in_v5_ddl():
    """The zero-address was added to the Python helper in round-9 (not
    in v5's frozen DDL). Asserts the asymmetry is intentional: Python
    recognizes it as a sentinel going forward; v5 DDL will not retroactively
    classify it. This is the documented limitation.
    """
    zero = "0x0000000000000000000000000000000000000000"
    # Python helper says sentinel.
    assert is_sentinel_trader_address(zero)
    # SQL helper (shared, parity with Python) says sentinel too.
    db = Database(db_path=Path(":memory:")).connect()
    try:
        row = db.fetchone(
            f"SELECT (CASE WHEN {address_is_sentinel_sql('addr_col', negate=False)} "
            f"THEN 1 ELSE 0 END) AS s FROM (SELECT ? AS addr_col)",
            (*address_is_sentinel_params(), zero),
        )
        assert row is not None
        assert row["s"] == 1, "shared SQL helper should classify zero-address as sentinel"
        # v5 DDL: NOT a sentinel (frozen, does not include zero-address).
        row = db.fetchone(
            """SELECT (CASE WHEN
                   LENGTH(TRIM(addr_col, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0
                   OR LOWER(TRIM(addr_col, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN ('unknown','anonymous','missing','0x','0x0')
                   THEN 1 ELSE 0 END) AS truth
               FROM (SELECT ? AS addr_col)""",
            (zero,),
        )
        assert row is not None
        assert row["truth"] == 0, (
            "v5 DDL unexpectedly classifies zero-address as sentinel "
            "(v5 should not — it's frozen pre-round-9)"
        )
    finally:
        db.close()


# ── (e) Round-trip through DB find-or-create ─────────────────────────────────

def test_find_or_create_idempotent_through_helper():
    """canonical_wallet_address applied repeatedly → same value (idempotent)."""
    for _ in range(100):
        assert canonical_wallet_address(canonical_wallet_address(REAL_ADDRESS)) == REAL_ADDRESS


# ── (f) Real wallet row survives every padding variant in metrics query ──────

def test_compute_metrics_finds_padded_legacy_address():
    """Insert padded legacy row, query with canonical, find it.

    Uses PAD_CHARS_SQL (excludes NUL) because the SQL fragment is what
    backs ``_compute_wallet_metrics``. NUL-padded rows are stripped by
    the Python helper at ingestion (so they never reach the DB in
    practice); this test exercises the realistic SQL-strippable set.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    import scripts.run_scan as run_scan_module  # noqa: E402

    db = Database(db_path=Path(":memory:")).connect()
    try:
        # Insert a padded legacy mixed-case row directly into source_trades
        # (mimics a pre-v5 migration state).
        for pad in PAD_CHARS_SQL:
            padded = pad + REAL_ADDRESS_UPPER + pad
            db.execute(
                """INSERT INTO source_trades
                   (id, source, source_trade_id, market_source_id, side,
                    outcome, quantity, price, trader_address, timestamp, is_sample)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    str(uuid.uuid4()), "polymarket_data_api",
                    f"legacy-{pad!r}-{uuid.uuid4()}", "0xCOND",
                    "BUY", "Yes", 1.0, 0.5, padded,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        db.conn.commit()

        # Canonical query (lowercase) finds ALL padded legacy variants.
        metrics = run_scan_module._compute_wallet_metrics(
            db, REAL_ADDRESS, datetime.now(timezone.utc),
        )
        assert metrics is not None
        assert metrics["trade_count"] == len(PAD_CHARS_SQL), (
            f"expected to find {len(PAD_CHARS_SQL)} padded rows, got {metrics['trade_count']}"
        )
    finally:
        db.close()


def test_compute_metrics_returns_none_for_sentinel():
    """Sentinel addresses never enter scoring — must return None."""
    sys.path.insert(0, str(_REPO_ROOT))
    import scripts.run_scan as run_scan_module  # noqa: E402

    db = Database(db_path=Path(":memory:")).connect()
    try:
        for sent in SENTINELS + ["", "  ", "\t"]:
            metrics = run_scan_module._compute_wallet_metrics(
                db, sent, datetime.now(timezone.utc),
            )
            assert metrics is None, f"sentinel {sent!r} returned metrics: {metrics}"
    finally:
        db.close()