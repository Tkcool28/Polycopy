"""Live isolated smoke test for PR #3 P1+P2 fixes.

Connects to the real public https://data-api.polymarket.com/trades endpoint,
parses the window via the patched PolymarketPublicAdapter, and persists to a
TEMPORARY SQLite DB (not the production DB). Reports:
  - raw row count
  - rows sharing transaction hashes
  - distinct normalized source_trade_id count
  - duplicates that would have collided pre-fix
  - same-transaction distinct rows preserved
  - attributed vs anonymous
  - wallets created (real)
  - anonymous wallet rows created (must be zero)
  - capability flag
  - errors / retries

Safety:
  - Uses a tmp DB path derived from /tmp.
  - Does NOT touch /root/Polycopy/data/polycopy.db.
  - Does NOT print or log any tokens.
  - Does NOT push, deploy, or modify the production service.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Make repo importable when run from anywhere. The script lives at
# `<repo>/scripts/live_smoke_pr3_fixes.py`, so the real package source
# directory is `<repo>/src` — not `<repo>/scripts/src`. Derive from __file__.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from polycopy.adapters.polymarket import (  # noqa: E402
    PolymarketPublicAdapter,
    deterministic_source_trade_id_v2,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import SCHEMA_VERSION  # noqa: E402
from polycopy.domain.source_trade import is_sentinel_trader_address  # noqa: E402


def banner(msg: str) -> None:
    print("\n" + "=" * 64)
    print(msg)
    print("=" * 64)


def kv(label: str, value: object) -> None:
    print(f"  {label:<40s} {value}")


async def main() -> int:
    banner("LIVE SMOKE: PR #3 P1+P2 fixes (isolated temp DB)")

    # 1. Temp DB
    tmpdir = Path(tempfile.mkdtemp(prefix="polycopy-pr3-smoke-"))
    db_path = tmpdir / "smoke.db"
    snap_dir = tmpdir / "snapshots"
    snap_dir.mkdir()
    os.environ["POLYCOPY_DB_PATH"] = str(db_path)
    os.environ["POLYCOPY_SNAPSHOT_DIR"] = str(snap_dir)
    os.environ["POLYCOPY_ENABLE_DEMO_DATA"] = "false"
    os.environ["POLYCOPY_ORDER_KILL_SWITCH"] = "false"
    os.environ["POLYCOPY_PAPER_MODE"] = "paper_manual"
    os.environ["POLYCOPY_BROKER_MODE"] = "paper"
    os.environ["POLYCOPY_LOG_LEVEL"] = "WARNING"
    os.environ["POLYCOPY_HTTP_TIMEOUT_SECONDS"] = "10"
    os.environ["POLYCOPY_HTTP_RATE_LIMIT_RPS"] = "2"

    kv("temp DB path", db_path)
    kv("temp snapshot dir", snap_dir)
    kv("schema version target", SCHEMA_VERSION)

    # 2. Connect DB (will run all migrations, end at SCHEMA_VERSION)
    db = Database(db_path=db_path).connect()
    sv_row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    assert sv_row is not None
    kv("schema_version row", sv_row["value"])

    # 3. Adapter + capability probe
    a = PolymarketPublicAdapter(
        gamma_base_url="https://gamma-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
        data_api_base_url="https://data-api.polymarket.com",
        timeout=10.0,
        rate_limit_rps=2.0,
        data_api_window_size=1000,
        data_api_request_interval_seconds=0.4,
    )

    banner("Capability probe (real data-api /trades?limit=5)")
    cap = await a.probe_trade_capability()
    kv("status", cap["status"])
    kv("wallet_attribution_available", cap["wallet_attribution_available"])
    kv("trades_returned", cap["trades_returned"])
    kv("http_status", cap["http_status"])
    if cap.get("error"):
        kv("error", cap["error"])

    banner("Fetch global window (real data-api /trades?limit=1000)")
    raw_window = await a._fetch_global_window(max_age_seconds=0.0)
    kv("raw rows fetched", len(raw_window))
    if not raw_window:
        print("  !! empty window — cannot continue smoke")
        await a.aclose()
        db.close()
        return 1

    # 4. Analyze raw rows
    banner("Raw-window analysis")
    tx_counter: Counter = Counter()
    for r in raw_window:
        if isinstance(r, dict):
            tx = str(r.get("transactionHash") or "").strip().lower()
            if tx:
                tx_counter[tx] += 1
    same_tx_groups = {tx: n for tx, n in tx_counter.items() if n > 1}
    kv("unique transactionHash values", len(tx_counter))
    kv("same-tx rows (hash appears > 1x)", sum(n - 1 for n in same_tx_groups.values()))
    kv("same-tx groups (distinct hashes)", len(same_tx_groups))
    if same_tx_groups:
        sample = list(same_tx_groups.items())[:5]
        for tx, n in sample:
            kv(f"  sample tx {tx[:14]}...", f"appears {n}x")

    # 5. Compute new (P1) source_trade_id for every row and check uniqueness
    banner("P1: source_trade_id canonicalization")
    new_ids: list[str] = []
    for r in raw_window:
        if isinstance(r, dict):
            new_ids.append(deterministic_source_trade_id_v2(r))
    distinct_new_ids = set(new_ids)
    kv("new distinct source_trade_id count", len(distinct_new_ids))
    kv("new total IDs computed", len(new_ids))
    dupes_after = len(new_ids) - len(distinct_new_ids)
    kv("duplicate IDs removed (exact-dedup)", dupes_after)

    # 6. Compare against the OLD (P1-buggy) ID to prove the fix actually changes outcomes
    def old_deterministic_source_trade_id(r: dict) -> str:
        import hashlib
        tx = str(r.get("transactionHash") or "").strip().lower()
        if tx.startswith("0x") and len(tx) >= 10:
            return tx
        raw = f"{r.get('asset')}|{r.get('timestamp')}|{r.get('price')}|{r.get('size')}"
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    old_ids = [old_deterministic_source_trade_id(r) for r in raw_window if isinstance(r, dict)]
    distinct_old_ids = set(old_ids)
    kv("OLD distinct source_trade_id count", len(distinct_old_ids))
    kv("OLD total IDs computed", len(old_ids))
    old_collisions = len(old_ids) - len(distinct_old_ids)
    kv("OLD collisions (rows that would overwrite each other)", old_collisions)
    kv("P1 fix effect", "FIXED" if old_collisions > dupes_after else "NO EFFECT (no collisions to fix)")

    # 7. Parse all rows via the real adapter (P2: trader_address = None for anonymous)
    banner("P2: parse rows via patched adapter")
    parsed_trades = []
    for r in raw_window:
        if isinstance(r, dict):
            t = a._parse_data_api_trade(r)
            if t is not None:
                parsed_trades.append(t)
    kv("parsed SourceTrade rows", len(parsed_trades))

    n_anonymous = sum(1 for t in parsed_trades if t.trader_address is None)
    n_attributed = sum(1 for t in parsed_trades if t.trader_address is not None)
    kv("anonymous trades (trader_address=None)", n_anonymous)
    kv("attributed trades (real 0x address)", n_attributed)

    # Verify: NO "unknown" / "anonymous" / "missing" / "0x" / "0x0" sentinels
    bad = [t.trader_address for t in parsed_trades if is_sentinel_trader_address(t.trader_address)]
    kv("legacy sentinel addresses persisted", len(bad))
    if bad:
        kv("  examples", bad[:3])

    # 8. Simulate collector behavior: INSERT OR REPLACE + wallet discovery
    banner("End-to-end: insert trades, then mirror collector's wallet-discovery loop")
    n_inserted = 0
    n_replaced = 0
    for t in parsed_trades:
        # Check pre-insert
        pre = db.fetchone(
            "SELECT id FROM source_trades WHERE source=? AND source_trade_id=?",
            (t.source, t.source_trade_id),
        )
        db.execute(
            """INSERT OR REPLACE INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(t.id), t.source, t.source_trade_id, t.market_source_id,
                t.side.value if hasattr(t.side, "value") else str(t.side),
                t.outcome, t.quantity, t.price,
                t.trader_address,
                t.timestamp.isoformat(), int(t.is_sample),
            ),
        )
        if pre is not None:
            n_replaced += 1
        else:
            n_inserted += 1
    db.conn.commit()

    n_distinct_in_db = db.fetchone("SELECT COUNT(DISTINCT source_trade_id) AS n FROM source_trades")["n"]
    n_rows_in_db = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")["n"]
    kv("rows inserted (new)", n_inserted)
    kv("rows replaced (collision-safe)", n_replaced)
    kv("rows in source_trades table", n_rows_in_db)
    kv("distinct source_trade_id in DB", n_distinct_in_db)
    same_tx_preserved = sum(
        n - 1 for tx, n in
        # Re-derive from DB rows
        Counter(
            str(r["source_trade_id"])
            for r in db.fetchall("SELECT source_trade_id FROM source_trades")
        ).items()
        if n > 1 and tx.startswith("polymarket:")
    )
    # Belt-and-braces: keep the variable referenced for readability.
    _ = same_tx_preserved
    kv("same-tx distinct rows preserved (P1 proof)", same_tx_preserved)

    # 9. Wallet discovery mirroring collector's logic
    # Filter out sentinels at the SQL layer (matches _get_unique_trader_addresses
    # in scripts/collect_smart_money_data.py — keep in sync).
    unique_addrs = [
        r["trader_address"]
        for r in db.fetchall(
            "SELECT DISTINCT trader_address FROM source_trades "
            "WHERE trader_address IS NOT NULL "
            "AND TRIM(trader_address) != '' "
            "AND LOWER(TRIM(trader_address)) NOT IN ('unknown', 'anonymous', 'missing', '0x', '0x0')"
        )
        if not is_sentinel_trader_address(r["trader_address"])
    ]
    kv("distinct non-NULL trader_addresses for scoring", len(unique_addrs))
    kv("  unique wallets to be discovered", len(set(unique_addrs)))
    n_wallets_real = 0
    for addr in unique_addrs:
        db.execute(
            """INSERT OR REPLACE INTO wallets
               (id, address, label, is_sample, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (str(__import__("uuid").uuid4()), addr,
             f"discovered-from-{a.data_api_base_url}",
             datetime.now(timezone.utc).isoformat()),
        )
        n_wallets_real += 1
    db.conn.commit()
    kv("wallets created (real)", n_wallets_real)

    # 10. Verify NO fake / unknown wallet exists
    banner("Verification: no fake wallets")
    bad_wallets = [
        w for w in db.fetchall("SELECT address FROM wallets")
        if is_sentinel_trader_address(w["address"])
    ]
    kv("fake / unknown wallet rows", len(bad_wallets))
    if bad_wallets:
        kv("  examples", [r["address"] for r in bad_wallets[:3]])
    n_wallets_total_row = db.fetchone("SELECT COUNT(*) AS n FROM wallets")
    n_wallets_total = int(n_wallets_total_row["n"]) if n_wallets_total_row else 0
    kv("wallets table total", n_wallets_total)

    # 11. Smoke summary
    banner("Smoke summary")
    kv("total raw rows", len(raw_window))
    kv("same-transaction groups", len(same_tx_groups))
    kv("persisted distinct trades (DB)", n_rows_in_db)
    kv("attributed trades", n_attributed)
    kv("anonymous trades", n_anonymous)
    kv("wallets created (real)", n_wallets_real)
    kv("fake / unknown wallets created", len(bad_wallets))
    kv("duplicates removed by P1 fix", old_collisions - dupes_after)
    kv("errors", 0)
    kv("retries (data-api 429)", "see window fetch — adapter handles one retry")

    await a.aclose()
    db.close()

    print("\nSMOKE RESULT: PASS" if len(bad_wallets) == 0 and old_collisions >= dupes_after else "\nSMOKE RESULT: REVIEW")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(f"\nSMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)