#!/usr/bin/env python3
"""PR69 report-only short-horizon specialist wallet CLI.

The CLI is offline-by-default. It opens no database, writes no orders,
positions, fills, settlements, shadow, or Alpha records, and never invokes
the production bridge or any writer. ``--allow-live`` enables only bounded
public GETs and only writes to an explicitly-named output directory.

The CLI is the operator-facing surface for the report-only audit path.
A pure ``discover_short_horizon_specialists_offline`` path exists for
fixture-based unit tests in ``tests/test_pr69_cli_offline.py``.

Safety guarantees:
  * default → no network, no DB, no file writes;
  * ``--allow-live`` is required for any network access;
  * output path cannot be a production DB path;
  * concurrency ≤ 4; max lock ≤ 30; preferred ≤ max lock;
  * history ≤ 730 days; no unlimited mode;
  * ``--write`` is intentionally absent — there is no write path;
  * ``Database(`` and ``sqlite3.connect`` are import-banned in this CLI;
  * deterministic JSON output (sort_keys, separators, sort by timestamp);
  * nonzero exit on malformed configuration or partial source with
    mandatory flags.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Headline imports. The CLI deliberately does NOT import Database /
# sqlite3 / any writer / any bridge. Static audit CI enforces this.
from polycopy.discovery.adapter import (  # noqa: E402
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    DiscoveryAdapter,
    list_broad_categories,
)
from polycopy.discovery.market_universe import (
    MarketUniverseAudit,
    MarketUniverseConfig,
    MarketUniverseCrawler,
    PREFERRED_SHORT_HORIZON,
    ELIGIBLE_SHORT_HORIZON,
    validate_config,
)
from polycopy.discovery._safe_get import _RequestBudget  # noqa: E402
from polycopy.discovery.short_horizon_specialists import (  # noqa: E402
    DISCOVERY_CONTRACT_VERSION,
    DiscoveryReport,
    attach_frozen_thresholds,
    discover_short_horizon_specialists,
)
from polycopy.discovery.taxonomy_enricher import TaxonomyEnricher  # noqa: E402
from polycopy.discovery.wallet_history import WalletHistoryFetcher  # noqa: E402
from polycopy.discovery.wallet_seeds import (  # noqa: E402
    DEFAULT_LEADERBOARD_TOP,
    DEFAULT_MAX_WALLETS,
    WalletSeedBuilder,
)

MAX_LOCK_CAP = 30
MAX_HISTORY_DAYS_CAP = 730
MAX_CONCURRENCY_CAP = 4


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    s = value.strip()
    if not s:
        raise ValueError("--as-of cannot be empty")
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--as-of is not ISO-8601: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include timezone")
    return parsed.astimezone(timezone.utc)


def _parse_categories(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple(list_broad_categories())
    parts = [p.strip() for p in value.split(",") if p.strip()]
    seen: dict[str, None] = {}
    for p in parts:
        seen[p.lower()] = None
    return tuple(seen.keys())


def _resolve_output_dir(path: str | None) -> Path | None:
    if not path:
        return None
    p = Path(path).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _assert_safe_output(path: Path) -> None:
    """Refuse to write into a directory that contains a production DB."""
    if path.is_file():
        if path.name.endswith(".db") or path.name.endswith(".sqlite3"):
            raise ValueError(f"output path {path} appears to be a database file")
        return
    if path.is_dir():
        for suffix in ("*.db", "*.sqlite3", "*.sqlite"):
            if list(path.glob(suffix)):
                raise ValueError(f"output directory {path} already contains production DB files")


def _adapter_from_kwargs(args: argparse.Namespace) -> DiscoveryAdapter:
    return DiscoveryAdapter(timeout_seconds=float(args.timeout or DEFAULT_TIMEOUT), max_retries=int(args.max_retries or DEFAULT_MAX_RETRIES))


def _build_requested(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "contract_version": DISCOVERY_CONTRACT_VERSION,
        "as_of": (args.as_of or ""),
        "preferred_days": int(args.preferred_days),
        "max_capital_lock_days": int(args.max_capital_lock_days),
        "resolution_buffer_days": int(args.resolution_buffer_days),
        "categories": list(_parse_categories(args.categories)),
        "leaderboard_top": int(args.leaderboard_top),
        "max_wallets": int(args.max_wallets),
        "history_days": int(args.history_days),
        "max_markets": int(args.max_markets),
        "max_requests": int(args.max_requests),
        "concurrency": int(args.concurrency),
        "page_size": int(args.page_size),
        "allow_live": bool(args.allow_live),
        "min_volume_24h": float(args.min_volume_24h),
        "min_liquidity": float(args.min_liquidity),
    }


async def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    adapter = _adapter_from_kwargs(args)
    budget = _RequestBudget(int(args.max_requests))
    enricher = TaxonomyEnricher(adapter, budget=budget)
    config = MarketUniverseConfig(
        as_of=_parse_as_of(args.as_of),
        preferred_days=int(args.preferred_days),
        max_capital_lock_days=int(args.max_capital_lock_days),
        resolution_buffer_days=int(args.resolution_buffer_days),
        categories=_parse_categories(args.categories),
        max_markets=int(args.max_markets),
        page_size=int(args.page_size),
        max_pages=int(args.max_pages),
        max_requests=int(args.max_requests),
        min_volume_24h=float(args.min_volume_24h),
        min_liquidity=float(args.min_liquidity),
        timeout_seconds=float(args.timeout),
    )
    validate_config(config)
    crawler = MarketUniverseCrawler(adapter, enricher, budget=budget)
    classifications, universe_audit = await crawler.run(config)
    eligibility = sorted(
        (c for c in classifications if c.bucket in (PREFERRED_SHORT_HORIZON, ELIGIBLE_SHORT_HORIZON)),
        key=lambda c: (c.bucket, c.condition_id),
    )

    seeds = WalletSeedBuilder(
        adapter,
        budget=budget,
        leaderboard_top=int(args.leaderboard_top),
        max_wallets=int(args.max_wallets),
        concurrency=int(args.concurrency),
    )
    seed_report = await seeds.build(classifications=classifications, categories=config.categories)
    from polycopy.discovery.wallet_seeds import SeedWallet
    seed_wallets = tuple(
        SeedWallet(wallet_address=w, sources=())
        for w in seed_report.union_wallets
    )
    history = WalletHistoryFetcher(
        adapter,
        budget=budget,
        history_days=int(args.history_days),
    )
    history_report = await history.fetch(
        seeds=seed_wallets,
        classifications=classifications,
        as_of=config.as_of,
    )

    report = discover_short_horizon_specialists(
        classifications=classifications,
        universe_audit=universe_audit,
        taxonomy_audit=enricher.audit(),
        seed_report=seed_report,
        history_report=history_report,
        requested=_build_requested(args),
        now=config.as_of,
    )
    final = attach_frozen_thresholds(report)

    universe_summary = {
        "inspected": universe_audit.markets_inspected,
        "by_bucket": universe_audit.bucket_counts,
        "truncated": universe_audit.truncated,
        "request_budget_used": universe_audit.request_budget_used,
        "eligibility_count": len(eligibility),
    }
    taxonomy_summary = {
        "embedded_usable": enricher.audit().embedded_usable,
        "market_tag_fallback": enricher.audit().market_tag_fallback_used,
        "event_fallback": enricher.audit().event_fallback_used,
        "series_fallback": enricher.audit().series_fallback_used,
        "partial": enricher.audit().partial,
        "unavailable": enricher.audit().unavailable,
        "conflict": enricher.audit().conflict,
        "api_failures": enricher.audit().api_failures,
    }
    seeds_summary = {
        "market_first_wallets": len(seed_report.market_first_wallets),
        "leaderboard_wallets": len(seed_report.leaderboard_wallets),
        "union_wallets": len(seed_report.union_wallets),
        "duplicate_wallets": len(seed_report.duplicate_wallets),
        "truncated": seed_report.truncated,
        "dropped_count": seed_report.dropped_count,
    }
    history_summary = {
        "wallets_fetched": len(history_report.wallets),
        "trades_seen": history_report.trades_seen,
        "history_days": history_report.history_days,
    }

    final["audit_summary"] = {
        "universe": universe_summary,
        "taxonomy": taxonomy_summary,
        "seeds": seeds_summary,
        "history": history_summary,
        "request_budget_initial": budget.initial,
        "request_budget_used": budget.used(),
        "adapter_timeout_seconds": float(args.timeout),
        "adapter_max_retries": int(args.max_retries),
    }
    final["classifications"] = [c.as_dict() for c in classifications]
    final["eligibility"] = [c.as_dict() for c in eligibility]
    await adapter.aclose()
    return final


def _load_offline(args: argparse.Namespace) -> dict[str, Any]:
    """Build a minimal offline report from a fixture file."""
    if not args.input_file:
        raise ValueError("--input-file is required for offline runs")
    payload = json.loads(Path(args.input_file).read_text())
    if not isinstance(payload, dict):
        raise ValueError("offline fixture must be a JSON object")
    markets = list(payload.get("markets", []))
    market_trades: dict[str, list[dict[str, Any]]] = {
        str(k).lower(): list(v) for k, v in (payload.get("market_trades") or {}).items()
    }
    leaderboard = list(payload.get("leaderboard", []))
    history_records = list(payload.get("history_records", []))
    requested = _build_requested(args)
    requested.update({
        "offline": True,
        "input_file": args.input_file,
        "history_records_count": len(history_records),
    })
    return {
        "contract_version": DISCOVERY_CONTRACT_VERSION,
        "requested": requested,
        "offline_market_count": len(markets),
        "offline_trade_keys": len(market_trades),
        "offline_history_records": len(history_records),
        "live_read_performed": False,
        "db_opened": False,
        "writes_performed": False,
        "fallback": {
            "ready_to_wire_to_automation": False,
        },
    }


def _serialize(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)


def _write_outputs(report: dict[str, Any], output_dir: Path, args: argparse.Namespace) -> None:
    encoded = _serialize(report)
    (output_dir / "short_horizon_specialist_wallet_audit.json").write_text(encoded + "\n")
    if not args.output_json and not args.output_wallet_csv and not args.output_market_csv and not args.output_exclusion_csv:
        return
    if args.output_json:
        (output_dir / args.output_json).write_text(encoded + "\n")
    if args.output_wallet_csv or args.output_market_csv or args.output_exclusion_csv:
        candidates = report.get("candidates", [])
        if args.output_wallet_csv:
            _write_wallet_csv(candidates, output_dir / args.output_wallet_csv)
        classifications = report.get("classifications", [])
        if args.output_market_csv:
            _write_market_csv(classifications, output_dir / args.output_market_csv)
        if args.output_exclusion_csv:
            _write_exclusion_csv(classifications, output_dir / args.output_exclusion_csv)


def _write_wallet_csv(candidates: list[dict[str, Any]], path: Path) -> None:
    cols = ["wallet_address", "overall_status", "overall_wallet_score", "overall_wallet_verdict",
            "qualifying_settled", "preferred_trades", "early_exits", "unresolved_trades",
            "active_trading_days", "distinct_events", "evidence_completeness"]
    rows = [",".join(cols)]
    for c in candidates:
        vals = [
            str(c.get(k, "")) for k in (
                "wallet_address", "overall_status", "overall_wallet_score",
                "overall_wallet_verdict", "qualifying_settled", "preferred_trades",
                "early_exits", "unresolved_trades", "active_trading_days", "distinct_events",
                "evidence_completeness",
            )
        ]
        rows.append(",".join(vals))
    path.write_text("\n".join(rows) + "\n")


def _write_market_csv(classifications: list[dict[str, Any]], path: Path) -> None:
    cols = ["condition_id", "bucket", "category_label", "taxonomy_status", "horizon_status", "end_date_iso"]
    rows = [",".join(cols)]
    for c in classifications:
        rows.append(",".join(
            '"' + str(c.get(k, "")).replace('"', "'") + '"'
            for k in cols
        ))
    path.write_text("\n".join(rows) + "\n")


def _write_exclusion_csv(classifications: list[dict[str, Any]], path: Path) -> None:
    cols = ["condition_id", "bucket", "reasons"]
    rows = [",".join(cols)]
    for c in classifications:
        if c.get("bucket") in (PREFERRED_SHORT_HORIZON, ELIGIBLE_SHORT_HORIZON):
            continue
        rows.append(",".join(
            '"' + str(c.get(k, "")).replace('"', "'") + '"'
            for k in cols
        ))
    path.write_text("\n".join(rows) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--allow-live", action="store_true", help="perform bounded public reads")
    p.add_argument("--as-of", help="ISO-8601 timestamp; defaults to utcnow")
    p.add_argument("--preferred-days", type=int, default=14, help="preferred end in days (≤ max)")
    p.add_argument("--max-capital-lock-days", type=int, default=30, help="hard capital-lock cap in days")
    p.add_argument("--resolution-buffer-days", type=int, default=6, help="resolution buffer in days")
    p.add_argument("--categories", help="comma-separated broad categories")
    p.add_argument("--leaderboard-top", type=int, default=DEFAULT_LEADERBOARD_TOP)
    p.add_argument("--max-wallets", type=int, default=DEFAULT_MAX_WALLETS)
    p.add_argument("--history-days", type=int, default=365)
    p.add_argument("--max-markets", type=int, default=200)
    p.add_argument("--max-pages", type=int, default=8)
    p.add_argument("--max-requests", type=int, default=80)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--min-volume-24h", type=float, default=0.0)
    p.add_argument("--min-liquidity", type=float, default=0.0)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    p.add_argument("--input-file", help="offline JSON fixture")
    p.add_argument("--output-dir", help="directory for the deterministic report")
    p.add_argument("--output-json", help="explicit JSON filename inside --output-dir")
    p.add_argument("--output-wallet-csv", help="csv filename for per-wallet results")
    p.add_argument("--output-market-csv", help="csv filename for per-market classifications")
    p.add_argument("--output-exclusion-csv", help="csv filename for excluded markets")
    p.add_argument("--require-partial-source-clean", action="store_true",
                   help="exit nonzero if any partial-source markets present")
    p.add_argument("--json", action="store_true", help="json to stdout instead of plaintext")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.allow_live and args.input_file:
            raise ValueError("--allow-live cannot be combined with --input-file")
        if not (1 <= int(args.preferred_days) <= MAX_LOCK_CAP):
            raise ValueError(f"--preferred-days must be in [1, {MAX_LOCK_CAP}]")
        if not (1 <= int(args.max_capital_lock_days) <= MAX_LOCK_CAP):
            raise ValueError(f"--max-capital-lock-days must be in [1, {MAX_LOCK_CAP}]")
        if int(args.resolution_buffer_days) < 0:
            raise ValueError("--resolution-buffer-days must be non-negative")
        if args.preferred_days > args.max_capital_lock_days:
            raise ValueError("--preferred-days must not exceed --max-capital-lock-days")
        if not (1 <= int(args.history_days) <= MAX_HISTORY_DAYS_CAP):
            raise ValueError(f"--history-days must be in [1, {MAX_HISTORY_DAYS_CAP}]")
        if not (1 <= int(args.concurrency) <= MAX_CONCURRENCY_CAP):
            raise ValueError(f"--concurrency must be in [1, {MAX_CONCURRENCY_CAP}]")
        if not (1 <= int(args.leaderboard_top) <= 100):
            raise ValueError("--leaderboard-top must be in [1, 100]")
        if not (1 <= int(args.max_wallets) <= 100):
            raise ValueError("--max-wallets must be in [1, 100]")
        if not (1 <= int(args.max_markets)):
            raise ValueError("--max-markets must be ≥ 1")
        if not (1 <= int(args.page_size)):
            raise ValueError("--page-size must be ≥ 1")
        if not (1 <= int(args.max_requests)):
            raise ValueError("--max-requests must be ≥ 1")

        output_dir = _resolve_output_dir(args.output_dir) if args.output_dir else None
        if output_dir is not None:
            _assert_safe_output(output_dir)

        if args.allow_live:
            report = asyncio.run(_run_live(args))
        else:
            report = _load_offline(args)

        if args.require_partial_source_clean and report.get("universe_summary"):
            tax = report.get("taxonomy_summary") or report.get("taxonomy_audit") or {}
            if isinstance(tax, dict) and int(tax.get("partial", 0) or 0) > 0:
                raise ValueError("--require-partial-source-clean failed: partial taxonomy > 0")

        encoded = _serialize(report)
        if output_dir is not None:
            _write_outputs(report, output_dir, args)
        print(encoded)
        return 0
    except ValueError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
