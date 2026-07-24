# Specialist qualification contract

This is the canonical eligibility contract shared by wallet scoring, category
scoring, the specialist-evidence cohort evaluator, readiness/status reporting,
persisted wallet decisions, behavior classification, and paper-signal eligibility.

## Wallet eligibility

A wallet may receive `copy_candidate` only when all current-evidence
requirements pass:

- wallet score is at least `75`;
- resolved markets are at least `30`;
- active trading days are at least `20`;
- distinct events are at least `15`;
- at least one supported category satisfies the category eligibility contract.

A missing or below-minimum global evidence value is insufficient evidence. The
wallet scorer returns `incomplete`, never `copy_candidate`, even if its numerical
score is at least 75. A score from 55 through 74.9999 with all global evidence
gates passing returns `watchlist`; a lower complete-evidence score retains the
existing `skip` verdict.

## Category eligibility

A supported category may receive `copy_candidate` only when all of these pass:

- category score is at least `75`;
- resolved category markets are at least `15`;
- distinct category events are at least `8`;
- category-active days are at least `10`.

Category weights, score components, and thresholds are independent of this
wallet-contract correction and remain unchanged.

## Behavior eligibility

Distinct resolved-market count is evidence depth: it establishes that the wallet
has enough observed resolved markets. It is not, by itself, behavioral
cross-category dispersion. A wallet can be directional across 30 or more
markets when it has positive directional evidence and no market-maker,
arbitrage, high-frequency, or conflicting two-sided/directional pattern.

`mixed` requires positive conflicting behavioral evidence (for example, both
material two-sided and dominant one-sided activity). High market count without
positive directional evidence remains `unknown` and is capped at `watchlist`;
high count alone no longer makes a wallet `mixed`.

## Current evidence, persistence, and versioning

Readiness/status recomputes current evidence; a historical persisted decision
cannot make a wallet GREEN on its own. The evaluator's dry-run and write modes
use the same wallet resolver. Eligibility failures are reported in dry-run and
status output and are persisted in `eligibility_failures_json` with the decision.

This is a bug fix to the existing wallet-score formula, so its formula version
remains `1`. The numerical formula, component weights, thresholds, evidence
fingerprint inputs, and category formula are unchanged. To prevent a historical
defective v1 decision from being silently reused, corrected wallet resolutions
include eligibility-contract revision `2` in their idempotency identity. Thus a
same-evidence corrected v1 decision is persisted once, while a replay of that
corrected identity is idempotent.

Behavior-classification semantics likewise retain their public formula version
while paper-signal idempotency includes behavior-classification contract revision
`2`. This prevents a historical paper-signal decision produced by the obsolete
high-market-count cap from colliding with a corrected current-evidence decision.
