"""Canonical wallet-identity normalization helpers.

Single source of truth for the rules that turn ANY wallet-address-looking
string into the canonical form used everywhere else in the codebase:

  1. ``None`` / empty / whitespace-only / legacy sentinel strings
     (``"unknown"``, ``"anonymous"``, ``"missing"``, ``"0x"``, ``"0x0"``)
     normalize to ``None`` — these are anonymous market-level observations,
     not real wallets, and MUST NOT be persisted into ``wallets`` or scored.
  2. Real 0x addresses normalize to ``lowercase(strip_all_whitespace)``.
     Stripping ALL ASCII whitespace (tab, LF, CR, VT, FF, NUL, space) — not
     just U+0020 — is required so a padded legacy address like
     ``"\\t0xAbCd...\\n"`` matches the canonical lowercase form.

Two surfaces are exported:

* :func:`canonical_wallet_address` — pure Python helper used at every
  ingestion / persistence / scoring boundary.
* :func:`address_column_normalized` — SQL expression generator that returns
  the canonical normalization of a column (use this in WHERE clauses that
  must match across case + padded variants).
* :func:`address_is_sentinel_sql` — returns the SQL fragment that mirrors
  :func:`is_sentinel_trader_address` (Python helper). Use this in WHERE
  clauses and in repository / migration code that needs to exclude
  sentinels and empty addresses.

The Python helper :func:`is_sentinel_trader_address` is re-exported from
:mod:`polycopy.domain.source_trade` for backward compatibility; new code
should import from either module — they share the same constants.
"""
from __future__ import annotations

from typing import Optional

from polycopy.domain.source_trade import (  # noqa: F401  (re-export)
    LEGACY_TRADER_ADDRESS_SENTINELS,
    is_sentinel_trader_address,
)


# ── Sentinel-set SQL fragment (sorted, comma-separated, single-quoted) ───────
# Stable ordering keeps migration diffs and EXPLAIN output reproducible.
# We use ? placeholders so callers can bind parameters in a fixed order,
# matching the pre-existing parameter-binding pattern in repository.py
# (which previously hard-coded the predicate). The ordering of these
# constants must match the placeholder order in ``address_is_sentinel_sql``.
_LEGACY_SENTINELS_SORTED: tuple[str, ...] = tuple(sorted(LEGACY_TRADER_ADDRESS_SENTINELS))


def address_column_normalized(column: str) -> str:
    """Return the SQL expression that yields the canonical form of ``column``.

    The returned expression strips the SAME whitespace set as
    :func:`canonical_wallet_address` EXCEPT NUL (see note below):

      * Stripped by both Python and SQL: tab, LF, CR, VT, FF, space.
      * Stripped by Python only (NOT by SQLite ``TRIM``/``REPLACE``):
        NUL (``X'00'``).

    Use it in WHERE clauses that need to match across case + padded
    legacy variants:

        WHERE <address_column_normalized("trader_address")> = ?

    NUL-strip limitation: SQLite's ``TRIM(col, X'00')`` and
    ``REPLACE(col, X'00', X'20')`` BOTH treat NUL as a C-string
    terminator and do not operate past the first NUL byte. There is no
    SQLite built-in that strips embedded NULs. In practice wallet
    addresses never contain embedded NULs (Polymarket, Ethereum, etc.
    all use ASCII-hex), and the Python helper strips NULs at every
    ingestion / persistence boundary — so a NUL-padded legacy row is
    normalized before it can hit ``source_trades``. The risk window is
    a pre-v5 legacy DB with NUL-padded rows imported by a non-Python
    tool, which the v5 migration's broader TRIM (tab/LF/CR/VT/FF/space)
    already handles for the migration cleanup.
    """
    quoted = '"' + column.replace('"', '""') + '"'
    # Strip chars: tab, LF, CR, VT, FF, space. (NUL excluded — SQLite
    # TRIM cannot remove it; Python handles NUL before it reaches here.)
    chars = "' ' || X'09' || X'0A' || X'0D' || X'0B' || X'0C'"
    return f"LOWER(TRIM({quoted}, {chars}))"


def address_is_sentinel_sql(column: str, *, negate: bool = True) -> str:
    """Return the SQL fragment that mirrors :func:`is_sentinel_trader_address`.

    By default the fragment evaluates to TRUE for NON-sentinel rows
    (i.e. real wallets) — the typical ``WHERE`` use. Pass ``negate=False``
    to flip the predicate (e.g. for DELETE statements that want to remove
    sentinels).

    The fragment uses ``?`` placeholders for the legacy sentinel set so
    callers bind parameters in the canonical order returned by
    :func:`address_is_sentinel_params`. The empty / whitespace-only check
    is inlined (no parameter needed).

    The sentinel set and the empty / whitespace-only check match the
    Python helper byte-for-byte (modulo the ASCII whitespace set above).
    """
    quoted = '"' + column.replace('"', '""') + '"'
    expr = address_column_normalized(column)
    placeholders = ", ".join("?" for _ in _LEGACY_SENTINELS_SORTED)
    predicate = (
        f"({quoted} IS NULL "
        f"OR {expr} = '' "
        f"OR {expr} IN ({placeholders}))"
    )
    if negate:
        return f"NOT {predicate}"
    return predicate


def address_is_sentinel_params() -> tuple[str, ...]:
    """Return the parameters that bind against :func:`address_is_sentinel_sql`.

    Order matches ``_LEGACY_SENTINELS_SORTED`` (alphabetical) so the
    fragment and its parameters cannot drift.
    """
    return _LEGACY_SENTINELS_SORTED


def canonical_wallet_address(value: Optional[str]) -> Optional[str]:
    """Return the canonical wallet-address form of ``value``.

    Mirrors :func:`address_column_normalized` for the common case
    (whitespace Python AND SQLite can both strip) and EXTENDS it for
    whitespace SQLite cannot strip (NUL):

      * ``None`` / empty / whitespace-only / legacy sentinel strings
        (``"unknown"``, ``"anonymous"``, ``"missing"``, ``"0x"``, ``"0x0"``)
        return ``None``.
      * Real addresses return ``lowercase(strip_all_whitespace)``.

    The Python strip matches the SQL strip set + NUL. The ``+NUL`` part
    is intentional defense — see :func:`address_column_normalized` for
    why the SQL helper cannot strip NUL. The Python helper is the
    source of truth for canonicalization at every ingestion boundary,
    so a NUL-padded row never reaches the DB; the SQL helper exists
    only for read-side matching.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    # Strip the SQL-strippable chars PLUS NUL (which SQL can't strip
    # but which never appears in legitimate wallet addresses).
    stripped = value.strip(" \t\n\r\x0b\x0c\x00")
    if not stripped:
        return None
    lowered = stripped.lower()
    if lowered in LEGACY_TRADER_ADDRESS_SENTINELS:
        return None
    return lowered


# Pre-built fragments for the two columns we care about most:
TRADER_ADDRESS_NORMALIZED = address_column_normalized("trader_address")
WALLETS_ADDRESS_NORMALIZED = address_column_normalized("address")


__all__ = [
    "LEGACY_TRADER_ADDRESS_SENTINELS",
    "address_column_normalized",
    "address_is_sentinel_params",
    "address_is_sentinel_sql",
    "canonical_wallet_address",
    "is_sentinel_trader_address",
    "TRADER_ADDRESS_NORMALIZED",
    "WALLETS_ADDRESS_NORMALIZED",
]