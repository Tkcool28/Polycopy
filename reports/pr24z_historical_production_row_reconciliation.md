# PR24Z Historical Production Row Reconciliation (read-only provenance)

## Historical run (commit 56fbd0ee67770af4df5c2dcd93d65eec4c2df583)
- mode: production-write
- live: True
- network_calls_attempted: 2
- network_calls_succeeded: 2
- wallet (redacted): 0x…0579
- raw_records: 25
- eligible_buy_records: 14
- inserted_rows: 14

## Production DB
- source_trades_total: 19
- matched_historical_rows: 14
- unmatched_historical_report_rows: 0
- unexpected_extra_matches: 0

## Reconciliation summary
- rows_examined: 14
- rows_all_fields_match: 14
- rows_with_mismatch: 0
- fixture_rows_found_in_production_set: 0
- all_14_proven_real_format: True
- all_14_report_db_match: True

## Fixture verification run (separate, NOT production)
- mode: safety-verification
- live: false
- network_calls_attempted: 0
- fixture_wallet: 0x1111... (redacted)
- valid_rows: 3
- write_was_null: true
- rows_written_to_production: 0

## Identity pipeline reconciliation
- historical_mapping_bug_confirmed: True
- historical_source_id_mislabeled_as_transaction_hash: True
- historical_strong_count: 0  historical_fallback_count: 25
- current_source_provided_count_for_14_rows: 14
- current_transaction_hash_count_for_14_rows: 0
- current_fallback_count_for_14_rows: 0
- current_ambiguous_count_for_14_rows: 0
- duplicate_rows_that_would_be_inserted: 0
- correction_verified: True

## HARD GATE — identity compatibility (legacy 14 rows, run vs REAL DB)
- checked: True
- historical_rows_expected: 14
- historical_rows_examined: 14
- canonical_matches: 14
- legacy_alias_matches: 0
- unmatched: 0
- rerun_would_insert: 0
- safe_for_future_production_write: True
- error: None

| # | existing_db_id | upstream_source_provided | corrected_canonical | existing_id_equals_corrected | legacy_alias_matches | recognized | would_insert |
|---|---|---|---|---|---|---|---|
| 1 | `polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90` | `polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90` | `polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90` | True | False | True | 0 |
| 2 | `polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75` | `polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75` | `polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75` | True | False | True | 0 |
| 3 | `polymarket:bd05d2c65e8c76b3017e2ae76b917311dfca8eb64d80b31c0c35f4dc6bdca54c` | `polymarket:bd05d2c65e8c76b3017e2ae76b917311dfca8eb64d80b31c0c35f4dc6bdca54c` | `polymarket:bd05d2c65e8c76b3017e2ae76b917311dfca8eb64d80b31c0c35f4dc6bdca54c` | True | False | True | 0 |
| 4 | `polymarket:bf5384d36afcfb132658b11e703d79a088f9b5821a02290d0652216fb6c8958f` | `polymarket:bf5384d36afcfb132658b11e703d79a088f9b5821a02290d0652216fb6c8958f` | `polymarket:bf5384d36afcfb132658b11e703d79a088f9b5821a02290d0652216fb6c8958f` | True | False | True | 0 |
| 5 | `polymarket:e9bd9ceacfb86240db805877269795d95773868a4fe497f92d76bd43cdd18536` | `polymarket:e9bd9ceacfb86240db805877269795d95773868a4fe497f92d76bd43cdd18536` | `polymarket:e9bd9ceacfb86240db805877269795d95773868a4fe497f92d76bd43cdd18536` | True | False | True | 0 |
| 6 | `polymarket:b3fbc4a16770ccde262588878b8d52169550098e92aa0c2e7ea54a22b3c99943` | `polymarket:b3fbc4a16770ccde262588878b8d52169550098e92aa0c2e7ea54a22b3c99943` | `polymarket:b3fbc4a16770ccde262588878b8d52169550098e92aa0c2e7ea54a22b3c99943` | True | False | True | 0 |
| 7 | `polymarket:1c69cda9e635a0e8e483c148c89c26cdbfa7953785f8c99f9b5d0ccbafd557a3` | `polymarket:1c69cda9e635a0e8e483c148c89c26cdbfa7953785f8c99f9b5d0ccbafd557a3` | `polymarket:1c69cda9e635a0e8e483c148c89c26cdbfa7953785f8c99f9b5d0ccbafd557a3` | True | False | True | 0 |
| 8 | `polymarket:afc7a199a17cfc3a12588e6457299e6ea8cc73d6d162f1883fe91f4d46077e86` | `polymarket:afc7a199a17cfc3a12588e6457299e6ea8cc73d6d162f1883fe91f4d46077e86` | `polymarket:afc7a199a17cfc3a12588e6457299e6ea8cc73d6d162f1883fe91f4d46077e86` | True | False | True | 0 |
| 9 | `polymarket:74f08b2cf54cd9170ed2a18d452198b1e3155cab6162fb96b2e428f3eb51a210` | `polymarket:74f08b2cf54cd9170ed2a18d452198b1e3155cab6162fb96b2e428f3eb51a210` | `polymarket:74f08b2cf54cd9170ed2a18d452198b1e3155cab6162fb96b2e428f3eb51a210` | True | False | True | 0 |
| 10 | `polymarket:d638d2140198a51f5796078389cff4ce936f983aa8522c5b28b6ec8d552a0607` | `polymarket:d638d2140198a51f5796078389cff4ce936f983aa8522c5b28b6ec8d552a0607` | `polymarket:d638d2140198a51f5796078389cff4ce936f983aa8522c5b28b6ec8d552a0607` | True | False | True | 0 |
| 11 | `polymarket:763644f6c36eba47a92ec9c4c01ec10c6dfb4d9ad87503a718c01f9a493f71ca` | `polymarket:763644f6c36eba47a92ec9c4c01ec10c6dfb4d9ad87503a718c01f9a493f71ca` | `polymarket:763644f6c36eba47a92ec9c4c01ec10c6dfb4d9ad87503a718c01f9a493f71ca` | True | False | True | 0 |
| 12 | `polymarket:ec5d86981161be068734dcde26c957a16c4584f4c2c8bbb1856b77b5f7d96ecd` | `polymarket:ec5d86981161be068734dcde26c957a16c4584f4c2c8bbb1856b77b5f7d96ecd` | `polymarket:ec5d86981161be068734dcde26c957a16c4584f4c2c8bbb1856b77b5f7d96ecd` | True | False | True | 0 |
| 13 | `polymarket:d84dbf96d8e00266b6de83a0604a0da425ab32441ab2d1c18223f51a5be7c106` | `polymarket:d84dbf96d8e00266b6de83a0604a0da425ab32441ab2d1c18223f51a5be7c106` | `polymarket:d84dbf96d8e00266b6de83a0604a0da425ab32441ab2d1c18223f51a5be7c106` | True | False | True | 0 |
| 14 | `polymarket:ccbcc9265c2d3578f6dc5732fef92f88b1f864d4ceaa380b6aa670afc5ec6d18` | `polymarket:ccbcc9265c2d3578f6dc5732fef92f88b1f864d4ceaa380b6aa670afc5ec6d18` | `polymarket:ccbcc9265c2d3578f6dc5732fef92f88b1f864d4ceaa380b6aa670afc5ec6d18` | True | False | True | 0 |

## 14-row field-for-field diff

| # | source_trade_id | all_match | placeholder | reasons |
|---|---|---|---|---|
| 1 | `polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90` | True | False | - |
| 2 | `polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75` | True | False | - |
| 3 | `polymarket:bd05d2c65e8c76b3017e2ae76b917311dfca8eb64d80b31c0c35f4dc6bdca54c` | True | False | - |
| 4 | `polymarket:bf5384d36afcfb132658b11e703d79a088f9b5821a02290d0652216fb6c8958f` | True | False | - |
| 5 | `polymarket:e9bd9ceacfb86240db805877269795d95773868a4fe497f92d76bd43cdd18536` | True | False | - |
| 6 | `polymarket:b3fbc4a16770ccde262588878b8d52169550098e92aa0c2e7ea54a22b3c99943` | True | False | - |
| 7 | `polymarket:1c69cda9e635a0e8e483c148c89c26cdbfa7953785f8c99f9b5d0ccbafd557a3` | True | False | - |
| 8 | `polymarket:afc7a199a17cfc3a12588e6457299e6ea8cc73d6d162f1883fe91f4d46077e86` | True | False | - |
| 9 | `polymarket:74f08b2cf54cd9170ed2a18d452198b1e3155cab6162fb96b2e428f3eb51a210` | True | False | - |
| 10 | `polymarket:d638d2140198a51f5796078389cff4ce936f983aa8522c5b28b6ec8d552a0607` | True | False | - |
| 11 | `polymarket:763644f6c36eba47a92ec9c4c01ec10c6dfb4d9ad87503a718c01f9a493f71ca` | True | False | - |
| 12 | `polymarket:ec5d86981161be068734dcde26c957a16c4584f4c2c8bbb1856b77b5f7d96ecd` | True | False | - |
| 13 | `polymarket:d84dbf96d8e00266b6de83a0604a0da425ab32441ab2d1c18223f51a5be7c106` | True | False | - |
| 14 | `polymarket:ccbcc9265c2d3578f6dc5732fef92f88b1f864d4ceaa380b6aa670afc5ec6d18` | True | False | - |
