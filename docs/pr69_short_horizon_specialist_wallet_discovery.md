# PR69 — Short-horizon specialist wallet discovery

PR69 adds a **report-only** discovery path for short-horizon Polymarket
specialists. It does not modify the approved-wallet list and cannot create
candidates, approvals, signals, orders, positions, or any database record.

## Policy and taxonomy

`polycopy.policy.short_horizon.evaluate_short_horizon` is the sole horizon
authority. Markets ending within 14 days are preferred; expected capital lock
is capped at 30 days, including the six-day resolution buffer. A missing,
invalid, ended, or long horizon fails closed. Historical reconciliation applies
the policy at the source trade timestamp. An early sale is not a resolution or
a win and cannot rescue a scheduled-long market.

`OfficialPolymarketTaxonomyResolverV1` accepts only explicit official market,
event, series category/root-tag evidence. Titles, questions, slugs, and
specific tags are never inferred as broad categories. Conflicts fail closed.

## Audit command

```bash
python scripts/audit_short_horizon_specialist_wallets.py
```

The default command performs **no network access, no DB access, and no file
writes**. For an offline deterministic fixture use `--input-file fixture.json`.

A bounded public-read audit is opt-in:

```bash
python scripts/audit_short_horizon_specialist_wallets.py \
  --allow-live --output-dir /tmp/polycopy-pr69-short-horizon-audit
```

Live mode reads at most 10 active Gamma markets, one page of at most 100 public
trades for each, and at most 20 leaderboard rows. It uses no authenticated
endpoint and writes only the explicit local output directory. The report
labels any unavailable realized-resolution evidence as incomplete.

## Scoring and reconciliation

The pure `discover_short_horizon_specialists` engine accepts caller-provided
market payloads, market-first trades, and leaderboard seeds. It does not open a
DB or make HTTP calls. Exact public trade identity is the only dedupe key;
there is no early-exit-as-win shortcut. Only eligible, official-taxonomy
reconciled evidence reaches the existing frozen `wallet_score_v1` and
`category_wallet_score_v1` implementations. Their formulas are reused without
modification; incomplete evidence remains incomplete.
