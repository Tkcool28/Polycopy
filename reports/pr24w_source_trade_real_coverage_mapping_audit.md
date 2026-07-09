SOURCE-TRADE REAL COVERAGE + TOKEN→CONDITION MAPPING AUDIT — READ ONLY / REPORT-ONLY

ready_to_wire_to_automation = False
ready_to_persist_decisions = False
ready_to_create_candidates = False

DB path inspected: /root/Polycopy/data/polycopy.db

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
  sample_placeholder: 4
  real_like: 1
  effective_real_usable_coverage: n=1

== Coverage bucket counts ==
  sample_placeholder: 4
  real_like_complete: 1

== Raw source side distribution (exact casing) ==
  buy: 4
  BUY: 1

== Canonical side distribution ==
  BUY: 5

== INGESTION SIDE NORMALIZATION AUDIT ==
  ingestion_side_inconsistency_present = True
  NOTE: mixed casing present; NOT backfilled (PR24T left existing rows).

== Identifier quality + readiness counts ==
  has_token_id: 1
  has_condition_id: 1
  both_token_and_condition: 1
  neither_token_nor_condition: 4
  non_condition_placeholder_market_id: 0
  pr24u_book_ready (/book, token_id): 1
  pr24v_gamma_ready (Gamma, conditionId): 1
  both_ready: 1
  neither_ready: 4
  token_to_condition_mapping_needed: 0

== Copyability evidence readiness counts ==
  blocked_no_usable_identifier: 4
  ready_both_paths: 1

== Token→Condition mapping feasibility (read-only) ==
  mapping_join_possible_via_market_outcomes = True
  market_outcomes_table_present = True
  market_outcomes_with_clob_token_id = 2
  markets_table_present = True
  resolve_trade_to_outcome_helper_exists = True
  mapping_helper_already_exists = True
  smallest_future_helper: Read-only helper map_token_to_condition_id(conn, token_id) -> Optional[str]: SELECT m.source_id FROM market_outcomes mo JOIN markets m ON m.id = mo.market_id WHERE mo.clob_token_id = ? LIMIT 1. This reuses the existing join; it is NOT a persistence writer and must not backfill production rows. PR24W does NOT implement it.
    note: token→condition join is feasible now: market_outcomes has 2 row(s) with a populated clob_token_id that can join to markets.source_id (conditionId).
    note: resolve_trade_to_outcome (engine/trade_resolution.py) already resolves token_id → market_outcomes → markets.source_id (conditionId) read-only; no new writer is required to READ the mapping.

== Ingestion gap summary ==
  source_trades total = 5; sample/placeholder = 4; real-like = 1; effective REAL usable coverage = n=1. Why n=1: 4 of 5 rows are seeded sample/placeholder rows (source='sample', is_sample=1, market_source_id='sample-market-*', token_id=NULL) created by scripts/run_scan.py _get_sample_trades; only test_trade_1 carries real identifiers (a conditionId-shaped market_source_id AND a real token_id). Real rows are NOT being collected at scale, and the sample rows were intentionally seeded and are still present. To unlock persistence/scoring, a real source_trade must have: canonical BUY side, parseable price, parseable size/quantity, AND at least one of (token_id for PR24U /book, conditionId-shaped market_source_id for PR24V Gamma). A token-only row additionally needs a token→condition mapping before PR24V market-state can attach.

== Findings ==
  [info] sample_placeholder_rows_present: Of 5 source_trades, 4 are seeded/sample/placeholder rows (non-condition market ids like 'sample-market-*' + NULL token_id + sample markers). Real usable production-like coverage is effectively n=1. This is report text only; the rows are NOT deleted, mutated, backfilled, or normalized.
    count=4
    evidence={"effective_real_usable_coverage": 1, "real_like_count": 1, "sample_placeholder_count": 4, "source_trade_count": 5}
    -> Treat the single real-like row (test_trade_1: real conditionId + real token_id) as the only currently-usable evidence target. A future ingestion PR should populate real wallet trades with both token_id and conditionId-shaped market_source_id for broader real coverage.
  [info] no_token_only_rows_currently: Current data has ZERO token-only rows (the 4 sample rows carry neither token nor condition; the 1 real-like row carries both). The token→condition mapping gap is a future risk, not a present blocker for the single usable row.
    count=0
    evidence={"both_count": 1, "has_condition_count": 1, "has_token_count": 1, "token_to_condition_mapping_needed_count": 0}
    -> No mapping writer needed for current data, but the helper should be added before real token-only ingestion lands.
  [warning] ingestion_side_inconsistency: source_trades.side contains multiple exact string forms for the same logical side (e.g. buy vs BUY). PR24T normalization guard handles future writes; existing production rows were intentionally not backfilled. This PR does not normalize them.
    count=5
    evidence={"canonical_side_distribution": {"BUY": 5}, "raw_side_distribution": {"BUY": 1, "buy": 4}}
    -> Leave existing rows as-is (no backfill per PR24T). Future writes normalize via normalize_side_for_persistence.
  [info] token_condition_mapping_feasibility: Token→condition mapping is ALREADY feasible read-only via the existing market_outcomes.clob_token_id → markets.source_id join (which resolve_trade_to_outcome already performs). PR24W does NOT implement a production mapping writer.
    count=2
    evidence={"mapping_helper_already_exists": true, "mapping_join_possible_via_market_outcomes": true, "market_outcomes_table_present": true, "market_outcomes_with_clob_token_id": 2, "markets_table_present": true, "notes": ["token\u2192condition join is feasible now: market_outcomes has 2 row(s) with a populated clob_token_id that can join to markets.source_id (conditionId).", "resolve_trade_to_outcome (engine/trade_resolution.py) already resolves token_id \u2192 market_outcomes \u2192 markets.source_id (conditionId) read-only; no new writer is required to READ the mapping."], "resolve_trade_to_outcome_helper_exists": true, "smallest_future_helper": "Read-only helper map_token_to_condition_id(conn, token_id) -> Optional[str]: SELECT m.source_id FROM market_outcomes mo JOIN markets m ON m.id = mo.market_id WHERE mo.clob_token_id = ? LIMIT 1. This reuses the existing join; it is NOT a persistence writer and must not backfill production rows. PR24W does NOT implement it."}
    -> Reuse the existing resolver; add a thin read-only map_token_to_condition_id helper when token-only rows appear. No backfill of production rows.

== Per-row report ==
  source_trade_id=sample-trade-sample-market-001-001 wallet=0xsample_trader_a_do_not_use market=sample-market-001 token=None side=buy
    canonical=BUY price=0.72 size=50.0 ts=2026-07-07T22:27:19.630960+00:00
    sample_status=sample_placeholder bucket=sample_placeholder id_quality=neither
    has_token=False has_condition=False pr24u_ready=False pr24v_ready=False both_ready=False
    mapping_needed=False copy_readiness=blocked_no_usable_identifier
    reason: seeded/sample/placeholder row: market_source_id is a non-condition placeholder and token_id is NULL; row carries sample markers (e.g. source/market/wallet/trade_id). is_sample flag = 1.
  source_trade_id=sample-trade-sample-market-001-002 wallet=0xsample_trader_b_do_not_use market=sample-market-001 token=None side=buy
    canonical=BUY price=0.7 size=30.0 ts=2026-07-07T22:27:19.630960+00:00
    sample_status=sample_placeholder bucket=sample_placeholder id_quality=neither
    has_token=False has_condition=False pr24u_ready=False pr24v_ready=False both_ready=False
    mapping_needed=False copy_readiness=blocked_no_usable_identifier
    reason: seeded/sample/placeholder row: market_source_id is a non-condition placeholder and token_id is NULL; row carries sample markers (e.g. source/market/wallet/trade_id). is_sample flag = 1.
  source_trade_id=sample-trade-sample-market-002-001 wallet=0xsample_trader_a_do_not_use market=sample-market-002 token=None side=buy
    canonical=BUY price=0.72 size=50.0 ts=2026-07-07T22:27:19.634150+00:00
    sample_status=sample_placeholder bucket=sample_placeholder id_quality=neither
    has_token=False has_condition=False pr24u_ready=False pr24v_ready=False both_ready=False
    mapping_needed=False copy_readiness=blocked_no_usable_identifier
    reason: seeded/sample/placeholder row: market_source_id is a non-condition placeholder and token_id is NULL; row carries sample markers (e.g. source/market/wallet/trade_id). is_sample flag = 1.
  source_trade_id=sample-trade-sample-market-002-002 wallet=0xsample_trader_b_do_not_use market=sample-market-002 token=None side=buy
    canonical=BUY price=0.7 size=30.0 ts=2026-07-07T22:27:19.634150+00:00
    sample_status=sample_placeholder bucket=sample_placeholder id_quality=neither
    has_token=False has_condition=False pr24u_ready=False pr24v_ready=False both_ready=False
    mapping_needed=False copy_readiness=blocked_no_usable_identifier
    reason: seeded/sample/placeholder row: market_source_id is a non-condition placeholder and token_id is NULL; row carries sample markers (e.g. source/market/wallet/trade_id). is_sample flag = 1.
  source_trade_id=test_trade_1 wallet=0xtest market=0xeb348b65a59bb2752d3dd10636d17de501df76a424e978e136d22e76d07c84e9 token=72753295727566659208677964635039361717871718602259295378609650323504626128275 side=BUY
    canonical=BUY price=0.4 size=100.0 ts=2026-07-01T00:00:00+00:00
    sample_status=real_like bucket=real_like_complete id_quality=both
    has_token=True has_condition=True pr24u_ready=True pr24v_ready=True both_ready=True
    mapping_needed=False copy_readiness=ready_both_paths
    reason: real-like: row carries real conditionId-shaped market_source_id and real token_id. (is_sample flag = 1, but the identifiers are production-shaped and resolvable; this is the single usable row.)

== Recommended next step ==
  PR24W is report-only. It proves effective real coverage is n=1 and that a token→condition mapping path already exists read-only. Next: (a) a guarded ingestion PR that populates REAL wallet trades with both token_id and conditionId-shaped market_source_id; (b) a thin read-only map_token_to_condition_id helper for token-only rows; (c) only then, a persistence/scoring PR that lands real coverage into candidates/signals. Do NOT wire automation or persist decisions until those land and are reviewed.

This report performs NO production writes: no decisions, candidates, paper signals, snapshots, orders, or positions. Default mode is read-only / dry-run / report-only.
