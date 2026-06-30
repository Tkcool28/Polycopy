# PR Review Policy

## Overview
This document defines the process for reviewing and merging pull requests (PRs) in the Polycopy project. It provides clear guidelines for validation, auditing, and decision-making to ensure consistent, high-quality codebase changes while protecting the stability of the system.

## Testing and Validation Sequence

### 1. Local Tests and Static Checks First
- **Action required**: Every PR contributor must run all local tests and static checks before creating a pull request
- **Required checks**: All tests, linting, type checking, and code formatting tools
- **Success criteria**: All automated tools pass with no failures or warnings

### 2. Consolidated Hermes Audit
- **Who performs it**: Assigned Hermes profile (casualfree by default for documentation PRs)
- **Scope**: Complete end-to-end validation including:
  - Functional test results from local run
  - Code quality checks that apply to the changed files (for example, Ruff and mypy for Python changes)
  - CI/CD pipeline compatibility
  - Documentation clarity and completeness
  - Compliance with project security and architecture guidelines
- **Deliverable**: Audit report with pass/fail status, detailed findings, and recommendations

### 3. SHA Freeze and Final Review
- **Action**: The Hermes auditor freezes the final SHA by recording the reviewed commit SHA in the PR or Kanban comment and then conducts one comprehensive review
- **Remote branch policy**: Pushing the candidate branch is allowed and expected; after the SHA is frozen, avoid rewriting or replacing that commit unless the review explicitly requests a new revision
- **Outcome**: Written approval or blocking rejection with clear reasoning

## Review Milestones

### Merge Blockers vs Optional Hardening
**Merge Blockers** (halt PR until resolved):
- Critical security vulnerabilities
- Breaking changes to public APIs
- Core functionality regressions
- Performance degradation beyond acceptable thresholds
- Failures in required validation checks

**Optional Hardening** (separate PRs if addressed):
- Code style improvements
- Non-critical documentation updates
- Unit tests where no functional change
- Security hardening non-issues

### Severity Definitions
1. **Critical (Blocker)**: System cannot function or security is compromised
2. **High**: Core functionality impaired or security risk present
3. **Medium**: Minor degradation but system remains functional
4. **Low**: Cosmetic, documentation, or nice-to-have improvements

## Validation Required

### Pre-Required and Existence Checks
- New code contains comprehensive tests
- Security guidelines are followed
- Architecture documentation is updated
- Project configuration files are valid
- Type safety checks pass
- Accessibility requirements are documented

### When to Split PRs
**Split Required**:
- Multiple logical changes require different review focus or different reviewers
- Unrelated findings are discovered during the review process
- Emergency or safety-critical fixes must be separated from normal feature work
- Schema/data-migration work, runtime behavior changes, documentation-only updates, and test-only cleanup would otherwise be bundled together
- The diff becomes too large to review confidently in one pass; prefer smaller focused PRs over hard line-count thresholds that rot over time

**Combine Allowed**:
- Related changes that fit within documentation scope
- Process improvements that logically flow from each other
- Multiple related documentation updates
- Testing framework improvements that enable other work

### When Codex Review is Worth the Cost
**Recommended** for:
- Complex code needing expert architectural review
- Critical security algorithms or implementations
- Performance-critical path optimizations
- Integration between major project components
- Non-standard solutions to complex problems
- Substantial testing refactoring affecting multiple components

**Not Recommended** for:
- Documentation-only changes
- Bug fixes in well-understood, simple code
- Straightforward refactoring without logic complexity
- Small-scale improvements without multi-system impact

## Handling Low-Severity Findings
**Immediate Action**:
- Document the finding in code comments
- Add visibility indicators for same-severity issues

**Escalation**:
- Review during comprehensive audit
- Separate PR may be created if pattern emerges
- Archive internal context for future reference

## Final Approval Process

### Documentation-Only Changes
- **Codex review**: Not requested by default; use at most one Codex review only when Todd explicitly approves it, and do not request Codex for routine documentation-only changes
- **Auditor approval**: Single Hermes audit is sufficient
- **Validation focus**: Documentation clarity, discoverability, and consistency with project workflow

### Non-Documentation Changes (If Applicable)
- **Pre-validation**: Thorough local testing first
- **Multiple audits**: One Hermes audit plus an optional single Codex review only when explicitly approved
- **Formal approval**: Written approval from project lead before merge

## Quality Gates

### Required Checks
Run the checks that apply to the changed paths. For the current Python/backend project, the usual baseline is:

```bash
# Local testing
pytest -q
ruff check .
mypy src/polycopy
git diff --check
```

Frontend or JavaScript checks should be added only when a future PR actually changes frontend assets or introduces that toolchain.

### Success Criteria
- All local tests pass
- No security or linting issues remain
- Documentation is clear and complete
- Code follows project standards
- No regressions detected

## Emergency Deployments
**Allowed only for**:
- Critical security fixes
- Production system failures
- Immediate safety concerns

**Process**:
1. Document emergency in standard PR format
2. Maintain standard review quality gates when possible
3. Document deviation justification
4. Schedule follow-up comprehensive review

## Conclusion
This policy ensures that all changes meet consistent quality standards while maintaining system stability. Each PR follows the defined sequence of validation and review, with clear escalation paths for issues that require additional attention.