TRADE COPYABILITY MARKET-STATE / END-TIME EVIDENCE BRIDGE — READ ONLY / DRY RUN / REPORT-ONLY

ready_to_wire_to_automation = False
ready_to_persist_decisions = False
ready_to_create_candidates = False
live_preview_enabled = False

DB path inspected: data/polycopy.db

== Production counts (read-only; must be unchanged by this PR) ==
  source_trades: 5
  trade_copyability_decisions: 0
  copy_candidates: 0
  paper_signal_decisions: 0
  candidate_price_snapshots: 0
  candidate_price_snapshot_levels: 0
  wallet_score_decisions: 1
  settlement_accounting_ledger: 0
  orders: 0
  positions: 0

== Source trades inspected: 5 ==
  eligible_for_bridge: 5
  ineligible_for_bridge: 0
  mappable_via_market_source_id: 1
  unmappable: 4
  resolved_market_metadata: 0
  sample_like_rows: 4 (seeded/placeholder; NOT deleted/mutated/backfilled)
  effective_real_production_like_coverage: n=1

== Metadata field availability ==
  market_state_available_count: 0
  end_time_available_count: 0
  seconds_to_market_end_available_count: 0

== Raw source side distribution (exact casing) ==
  buy: 4
  BUY: 1

== Canonical side distribution ==
  BUY: 5

== INGESTION SIDE NORMALIZATION AUDIT ==
  ingestion_side_inconsistency_present = True
  NOTE: mixed casing present; NOT backfilled (PR24T left existing rows).

== Identifier mapping status ==
  sample_skipped: 4
  mappable: 1

== Skip reasons (rows) ==
  market_identifier_not_condition_id: 4
  missing_token_id: 4

== Findings ==
  [warning] ingestion_side_inconsistency: source_trades.side contains multiple exact string forms for the same logical side (e.g. buy vs BUY). PR24T normalization guard handles future writes; existing production rows were intentionally not backfilled. This PR does not normalize them.
    count=5
    evidence={"canonical_side_distribution": {"BUY": 5}, "raw_side_distribution": {"BUY": 1, "buy": 4}}
    -> Leave existing rows as-is (no backfill per PR24T). Future writes normalize via normalize_side_for_persistence.
  [info] source_trade_sample_data_present: Of 5 source_trades inspected, 4 appear to be seeded/sample/placeholder rows (wallet/market/trade_id contain sample markers such as 0xsample_trader_*_do_not_use and sample-market-*). Real usable production-like market-state coverage is effectively n=1. This is report text only; the rows are NOT deleted, mutated, backfilled, or normalized.
    count=4
    evidence={"effective_real_coverage_n": 1, "sample_like_row_count": 4, "source_trade_count": 5}
    -> Treat the single non-sample mappable row (test_trade_1) as the only currently-real market-state resolution target. A future ingestion PR should populate real rows with conditionId-shaped market_source_id for broader coverage.
  [info] identifier_mapping_status_summary: Per-row market-identifier mapping status. 'mappable' means a conditionId-shaped market_source_id is present and resolvable via Gamma get_market. 'unresolvable_token_id_only' means only token_id is present (Gamma get_market keys on conditionId; a deferred token->condition helper is required). 'sample_skipped' / 'missing_identifier' are excluded.
    count=5
    evidence={"mapping_status_counts": {"mappable": 1, "sample_skipped": 4}}
    -> Persist/ingest a conditionId-shaped market_source_id per real trade; add a token->condition mapping helper if token_id-only resolution is needed.
  [info] dry_run_offline_no_network: Default dry-run mode performed NO network fetch. Mappability and field-availability were proven structurally against source_trades. Use --allow-live-preview to perform a real read-only Gamma get_market fetch (still non-persisting).
    -> Run with --allow-live-preview for a live metadata preview.

== Per-row report (exact PR24V required fields) ==
  source_trade_id=sample-trade-sample-market-001-001 wallet=0xsample_trader_a_do_not_use market=sample-market-001 token=None side=buy
    eligibility=eligible mappability=sample_skipped lookup_id=sample-market-001
    market_active=False:None closed=False:None resolved=False:None
    end_time=False:None seconds_to_end=False:None fetched_at=None
    mapping_status=unresolvable_non_condition_id pr24u_pr24s_combinable=False
    skip_reason=market_identifier_not_condition_id;missing_token_id
    note: no resolvable market identifier present; market-state metadata could not be fetched (honest: not invented)
  source_trade_id=sample-trade-sample-market-001-002 wallet=0xsample_trader_b_do_not_use market=sample-market-001 token=None side=buy
    eligibility=eligible mappability=sample_skipped lookup_id=sample-market-001
    market_active=False:None closed=False:None resolved=False:None
    end_time=False:None seconds_to_end=False:None fetched_at=None
    mapping_status=unresolvable_non_condition_id pr24u_pr24s_combinable=False
    skip_reason=market_identifier_not_condition_id;missing_token_id
    note: no resolvable market identifier present; market-state metadata could not be fetched (honest: not invented)
  source_trade_id=sample-trade-sample-market-002-001 wallet=0xsample_trader_a_do_not_use market=sample-market-002 token=None side=buy
    eligibility=eligible mappability=sample_skipped lookup_id=sample-market-002
    market_active=False:None closed=False:None resolved=False:None
    end_time=False:None seconds_to_end=False:None fetched_at=None
    mapping_status=unresolvable_non_condition_id pr24u_pr24s_combinable=False
    skip_reason=market_identifier_not_condition_id;missing_token_id
    note: no resolvable market identifier present; market-state metadata could not be fetched (honest: not invented)
  source_trade_id=sample-trade-sample-market-002-002 wallet=0xsample_trader_b_do_not_use market=sample-market-002 token=None side=buy
    eligibility=eligible mappability=sample_skipped lookup_id=sample-market-002
    market_active=False:None closed=False:None resolved=False:None
    end_time=False:None seconds_to_end=False:None fetched_at=None
    mapping_status=unresolvable_non_condition_id pr24u_pr24s_combinable=False
    skip_reason=market_identifier_not_condition_id;missing_token_id
    note: no resolvable market identifier present; market-state metadata could not be fetched (honest: not invented)
  source_trade_id=test_trade_1 wallet=0xtest market=0xeb348b65a59bb2752d3dd10636d17de501df76a424e978e136d22e76d07c84e9 token=72753295727566659208677964635039361717871718602259295378609650323504626128275 side=BUY
    eligibility=eligible mappability=mappable lookup_id=0xeb348b65a59bb2752d3dd10636d17de501df76a424e978e136d22e76d07c84e9
    market_active=False:None closed=False:None resolved=False:None
    end_time=False:None seconds_to_end=False:None fetched_at=None
    mapping_status=resolved_via_market_source_id pr24u_pr24s_combinable=True
    note: offline dry-run; no Gamma fetch performed

== Recommended next step ==
  PR24V proves the market-state / end-time evidence path is wireable by REUSING PolymarketPublicAdapter.get_market (Gamma /markets/{conditionId}) and reports which identifiers can resolve. Next: (a) a guarded persistence writer landing these fields into PR24S SnapshotEvidenceResult + PR24U row report (both already carry them, currently None), and (b) a token->condition mapping helper for token_id-only rows. Do not wire automation or persist decisions until those land and are reviewed.

This report performs NO production writes: no market-state persistence, decisions, candidates, paper signals, orders, or positions. Default mode is dry-run. Live preview (--allow-live-preview) is read-only and non-persisting.
