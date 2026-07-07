"""Bounded query iteration helpers (PR24B).

These helpers replace ad-hoc ``db.fetchall(...)`` patterns in the
operational scripts that load *unbounded* rows into Python lists. Each
helper returns a generator/iterator that walks the result set in
fixed-size batches, capping peak memory at ``batch_size`` rows.

Design choices
--------------
- **Cursor-based, not ``fetchmany`` round-trips.** Each batch opens a
  fresh ``Cursor`` on the same connection so the per-row Python overhead
  stays small. Cursors are closed when the iterator is exhausted or
  garbage-collected.
- **Keyset pagination is preferred for hot paths with monotonic keys.**
  ``iter_keyset_batches`` uses ``WHERE <col> < ? ORDER BY <col> DESC``
  so page N is O(batch_size) regardless of how deep into the result
  set the caller is — vs. ``LIMIT n OFFSET k`` which is O(k) on every
  page. Required for ``source_trades`` where the per-wallet loop
  previously loaded every trade ever attributed to that wallet.
- **LIMIT/OFFSET helper provided for small / cold paths only.**
  :func:`iter_offset_batches` is exposed because the project already
  uses LIMIT/OFFSET in the API layer (see PR #3 sentinel-pagination
  fix), and there are read paths where OFFSET is genuinely fine
  (browsing wallets in paged UI). The hot paths must use keyset.
- **No transaction held while iterating.** Each helper runs in the
  connection's implicit transaction (``sqlite3`` default). For long
  reads, the caller can wrap in ``BEGIN`` ... ``COMMIT`` themselves if
  isolation matters — the helpers do not start or end transactions.

These helpers are intentionally minimal: no schema knowledge, no
SQL rewriting, no connection management. They are wrappers around
``Database.conn.execute(...)`` and a manually-closed ``Cursor``.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# Sensible default batch size for per-row streaming. 200 rows keeps
# each Python list under a few hundred KiB even for fat schemas, while
# letting SQLite deliver chunks in a single page query.
DEFAULT_BATCH_SIZE = 200


def _close_cursor(cursor: sqlite3.Cursor) -> None:
    """Best-effort cursor close — never raises."""
    try:
        cursor.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


def iter_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[object] = (),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[sqlite3.Row]:
    """Stream rows one at a time via a server-side cursor.

    Internally uses :func:`iter_batches` with ``batch_size``; callers
    that need batch granularity should call :func:`iter_batches`
    directly. Provided as the ergonomic default for hot paths that
    previously did ``for row in db.fetchall(...):``.

    The returned iterator MUST be closed (or fully drained) before any
    write transaction that touches the same tables starts, otherwise
    ``database is locked`` may surface.
    """
    for batch in iter_batches(conn, sql, params, batch_size=batch_size):
        yield from batch


def iter_batches(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[object] = (),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[List[sqlite3.Row]]:
    """Yield successive batches of up to ``batch_size`` rows.

    Args:
        conn: an open ``sqlite3.Connection``.
        sql: a SELECT statement. Any ``LIMIT``/``OFFSET`` already in
            the SQL is respected; this helper does *not* modify the
            query. For paginated reads, prefer :func:`iter_keyset_batches`.
        params: parameters to bind to ``sql``.
        batch_size: rows per batch. Clamped to >= 1.

    Yields:
        Lists of ``sqlite3.Row`` of length ≤ ``batch_size``. The final
        batch may be shorter.

    Notes:
        The cursor is opened at the start and closed when the
        generator is exhausted, garbage-collected, or explicitly closed
        (``gen.close()``). Callers that exit early SHOULD call
        ``gen.close()`` to release the cursor promptly — otherwise GC
        handles it eventually.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    cursor: Optional[sqlite3.Cursor] = None
    try:
        cursor = conn.execute(sql, tuple(params))
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield rows
    finally:
        if cursor is not None:
            _close_cursor(cursor)


def iter_keyset_batches(
    conn: sqlite3.Connection,
    *,
    base_sql: str,
    keyset_col: str,
    last_value: object = None,
    extra_where: str = "",
    base_params: Sequence[object] = (),
    batch_size: int = DEFAULT_BATCH_SIZE,
    descending: bool = True,
) -> Iterator[List[sqlite3.Row]]:
    """Yield successive batches of rows using keyset (seek) pagination.

    A monotonic key (``timestamp DESC``, autoincrement ``id``, etc.)
    makes each page O(batch_size) regardless of depth, unlike
    ``LIMIT n OFFSET k`` which is O(k) per page.

    Args:
        conn: open ``sqlite3.Connection``.
        base_sql: the SELECT body WITHOUT the ``WHERE``/``ORDER BY``
            clauses (those are added here). Example::

                "SELECT id, trader_address, price, timestamp
                 FROM source_trades"

        keyset_col: the monotonic column to seek on. Quoted in the
            generated SQL — caller must guarantee the column name is
            safe (no user-supplied SQL fragments).
        last_value: the previous page's last ``keyset_col`` value. Pass
            ``None`` (the default) to start from the top of the
            ordered result set; the helper will walk the entire result
            set in pages, resuming from the last row of each batch.
        extra_where: optional extra WHERE fragment appended AFTER the
            keyset predicate. Must use SQL placeholders for any
            runtime parameters; those placeholders are bound first.
        base_params: parameters bound to ``extra_where`` placeholders.
        descending: ``True`` for ``ORDER BY <col> DESC`` (newest first);
            ``False`` for ascending.

    Yields:
        Successive batches of rows. Each batch has length ≤
        ``batch_size``. The generator stops when a batch comes back
        shorter than ``batch_size`` (final page).

    Notes:
        The keyset predicate uses ``<`` (descending) or ``>`` (ascending).
        Equal-to-last_value rows from the same page boundary are NOT
        returned by the next page — they were already on the previous
        page. Tie-breaking for non-unique keys is the caller's
        responsibility.

        When ``last_value`` is ``None`` the helper walks the entire
        result set. When ``last_value`` is supplied (typically for a
        resume), the helper yields a single page starting after that
        value. Callers that need to drive multi-page resumes manually
        should call this once per page, passing the previous page's
        last row's ``keyset_col`` value back as ``last_value``.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    order = "DESC" if descending else "ASC"
    op = "<" if descending else ">"

    if last_value is None:
        # Walk the entire result set, page by page. The keyset predicate
        # starts as "1=1" so we get the first page; after that we resume
        # using the last row's keyset_col value.
        current_last: object = None
        while True:
            if current_last is None:
                predicate = "1=1"
                keyset_params: Tuple[object, ...] = ()
            else:
                predicate = f"{keyset_col} {op} ?"
                keyset_params = (current_last,)
            where_clause = predicate
            if extra_where:
                where_clause = f"{where_clause} {extra_where}"
            sql = (
                f"{base_sql} "
                f"WHERE {where_clause} "
                f"ORDER BY {keyset_col} {order} "
                f"LIMIT ?"
            )
            params: Tuple[object, ...] = (
                *keyset_params,
                *tuple(base_params),
                batch_size,
            )
            # Manual page fetch so we can detect "no rows" vs "short batch".
            cursor = conn.execute(sql, params)
            try:
                rows = cursor.fetchall()
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    pass
            if not rows:
                return
            yield rows
            if len(rows) < batch_size:
                return
            current_last = rows[-1][keyset_col]
    else:
        # Single-page resume from a known keyset value.
        predicate = f"{keyset_col} {op} ?"
        keyset_params = (last_value,)
        where_clause = predicate
        if extra_where:
            where_clause = f"{where_clause} {extra_where}"
        sql = (
            f"{base_sql} "
            f"WHERE {where_clause} "
            f"ORDER BY {keyset_col} {order} "
            f"LIMIT ?"
        )
        params = (*keyset_params, *tuple(base_params), batch_size)
        cursor = conn.execute(sql, params)
        try:
            rows = cursor.fetchall()
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass
        if rows:
            yield rows


def iter_offset_batches(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[object] = (),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[List[sqlite3.Row]]:
    """Yield batches via LIMIT/OFFSET pagination.

    Only suitable for small / cold read paths. ``OFFSET`` is O(k) per
    page so deeply paginated reads over large tables will be slow. Use
    :func:`iter_keyset_batches` when a monotonic key is available.

    Args:
        conn: open ``sqlite3.Connection``.
        sql: the full SELECT statement. The helper strips a trailing
            ``;`` for safety; do NOT include ``LIMIT``/``OFFSET``
            yourself — they are appended here.
        params: parameters bound to ``sql`` placeholders.
        batch_size: rows per batch. Clamped to >= 1.

    Yields:
        Successive batches of rows.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    cleaned = sql.rstrip().rstrip(";").rstrip()
    offset = 0
    while True:
        page_sql = f"{cleaned} LIMIT ? OFFSET ?"
        page_params: Tuple[object, ...] = (*tuple(params), batch_size, offset)
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = conn.execute(page_sql, page_params)
            rows = cursor.fetchall()
        finally:
            if cursor is not None:
                _close_cursor(cursor)
        if not rows:
            return
        yield rows
        if len(rows) < batch_size:
            return
        offset += batch_size


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "iter_batches",
    "iter_keyset_batches",
    "iter_offset_batches",
    "iter_rows",
]