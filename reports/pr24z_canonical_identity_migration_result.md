# PR24Z canonical identity migration result

- ok: True
- state: ALL_LEGACY
- rows_updated: 14
- already_migrated: False
- trust_gate: found=14 immutable_matches=14 immutable_mismatches=0
- dependency_audit_safe: True
- wallet_score_decisions: otherwise trade-linked
- marker_created: True
- error: None

## Superseding historical report pointer
The original PR24Z write evidence records the legacy IDs written at that time. Those IDs are historical, not current canonical IDs. The authoritative mapping lives in `reports/pr24z_canonical_identity_migration_mapping.csv`; the authoritative result lives in `reports/pr24z_canonical_identity_migration_result.json`.
