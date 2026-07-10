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
- canonical_matches: 0
- legacy_alias_matches: 14
- unmatched: 0
- rerun_would_insert: 0
- safe_for_future_production_write: True
- error: None

| # | existing_db_id | upstream_source_provided | corrected_canonical | existing_id_equals_corrected | legacy_alias_matches | recognized | would_insert |
|---|---|---|---|---|---|---|---|
| 1 | `polymarket:11ae80be5e1566fda292d78b0150629ef29c828a1ed3c6df81bc00ba8b9ffe90` | `polymarket:e0c9d495b892a136f1053473e3cb96d4a721e1fce7bb46bf1019d911ad441dbb` | `polymarket:e0c9d495b892a136f1053473e3cb96d4a721e1fce7bb46bf1019d911ad441dbb` | True | True | True | 0 |
| 2 | `polymarket:9b9b74c36aec602b1fd5dc43d10664b8ecee3254a8af9c1b65bc92d333e26a75` | `polymarket:9b811fe6d9f115c5c23d9e73c960e2566ad7e442cfff3d5215d8c16a15705671` | `polymarket:9b811fe6d9f115c5c23d9e73c960e2566ad7e442cfff3d5215d8c16a15705671` | True | True | True | 0 |
| 3 | `polymarket:bd05d2c65e8c76b3017e2ae76b917311dfca8eb64d80b31c0c35f4dc6bdca54c` | `polymarket:781e5be438576fb655f4eb233474ad0da25d5213d432c100ce574dcb0a8bbb32` | `polymarket:781e5be438576fb655f4eb233474ad0da25d5213d432c100ce574dcb0a8bbb32` | True | True | True | 0 |
| 4 | `polymarket:bf5384d36afcfb132658b11e703d79a088f9b5821a02290d0652216fb6c8958f` | `polymarket:a5a0a5c73e49717c2e3c062fc01edb8f24ff64437d76972ab3271c505e4083f6` | `polymarket:a5a0a5c73e49717c2e3c062fc01edb8f24ff64437d76972ab3271c505e4083f6` | True | True | True | 0 |
| 5 | `polymarket:e9bd9ceacfb86240db805877269795d95773868a4fe497f92d76bd43cdd18536` | `polymarket:c113f439b543445b928acef06737eec10caf4c5239261ac31df5e2ec73cbea33` | `polymarket:c113f439b543445b928acef06737eec10caf4c5239261ac31df5e2ec73cbea33` | True | True | True | 0 |
| 6 | `polymarket:b3fbc4a16770ccde262588878b8d52169550098e92aa0c2e7ea54a22b3c99943` | `polymarket:ef34763e25be92c60b4d16a26d696fc819ea059ac374c23660bec1445efc7153` | `polymarket:ef34763e25be92c60b4d16a26d696fc819ea059ac374c23660bec1445efc7153` | True | True | True | 0 |
| 7 | `polymarket:1c69cda9e635a0e8e483c148c89c26cdbfa7953785f8c99f9b5d0ccbafd557a3` | `polymarket:de8b24e7e5b04c4840ee889d13cde943f0995f9eecef633712f9be256f406a22` | `polymarket:de8b24e7e5b04c4840ee889d13cde943f0995f9eecef633712f9be256f406a22` | True | True | True | 0 |
| 8 | `polymarket:afc7a199a17cfc3a12588e6457299e6ea8cc73d6d162f1883fe91f4d46077e86` | `polymarket:87750f305c61d53ed0c80b86f0e62a8a3d281ce67fcbe8c6ba821ec240e85334` | `polymarket:87750f305c61d53ed0c80b86f0e62a8a3d281ce67fcbe8c6ba821ec240e85334` | True | True | True | 0 |
| 9 | `polymarket:74f08b2cf54cd9170ed2a18d452198b1e3155cab6162fb96b2e428f3eb51a210` | `polymarket:e6dfc3d111e9f8996097206c2a2477ccf5ef0ec2075c94f05155fbcbb2961ec8` | `polymarket:e6dfc3d111e9f8996097206c2a2477ccf5ef0ec2075c94f05155fbcbb2961ec8` | True | True | True | 0 |
| 10 | `polymarket:d638d2140198a51f5796078389cff4ce936f983aa8522c5b28b6ec8d552a0607` | `polymarket:fa060215882d411ad30307f3877f07a49b9dac4f83d4b3cc89130017f53a75be` | `polymarket:fa060215882d411ad30307f3877f07a49b9dac4f83d4b3cc89130017f53a75be` | True | True | True | 0 |
| 11 | `polymarket:763644f6c36eba47a92ec9c4c01ec10c6dfb4d9ad87503a718c01f9a493f71ca` | `polymarket:ec13850bbc33b1a1cc3174fcadd5f01bbb769101a8055dc43ceee734b94181e3` | `polymarket:ec13850bbc33b1a1cc3174fcadd5f01bbb769101a8055dc43ceee734b94181e3` | True | True | True | 0 |
| 12 | `polymarket:ec5d86981161be068734dcde26c957a16c4584f4c2c8bbb1856b77b5f7d96ecd` | `polymarket:bbe760b9f65f383737473acc2d25fadc1d7152bb6759d4fc4320311b60564481` | `polymarket:bbe760b9f65f383737473acc2d25fadc1d7152bb6759d4fc4320311b60564481` | True | True | True | 0 |
| 13 | `polymarket:d84dbf96d8e00266b6de83a0604a0da425ab32441ab2d1c18223f51a5be7c106` | `polymarket:81965b4867e0f41564a0e938006678638a4d7c5fb643c5bf8e33aed87bd18ebd` | `polymarket:81965b4867e0f41564a0e938006678638a4d7c5fb643c5bf8e33aed87bd18ebd` | True | True | True | 0 |
| 14 | `polymarket:ccbcc9265c2d3578f6dc5732fef92f88b1f864d4ceaa380b6aa670afc5ec6d18` | `polymarket:a2d2bdb1c64c651760ff93d4fc2c6e91850bdf6adc729c4edbf9ed3d3f2e9d38` | `polymarket:a2d2bdb1c64c651760ff93d4fc2c6e91850bdf6adc729c4edbf9ed3d3f2e9d38` | True | True | True | 0 |

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
