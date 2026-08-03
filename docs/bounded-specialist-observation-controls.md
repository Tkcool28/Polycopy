# Bounded specialist observation controls

## Purpose and frozen policy

This module is a **filesystem-only control plane** for a future, bounded observation. It represents one to five watches; the production template freezes the approved five `sew_...` IDs. The standard window is 21 days with day 7, 14, and 21 checkpoints. Its intended future cadence is four BUY-only collection runs daily (00:00, 06:00, 12:00, 18:00 America/Denver) and inline honest enrichment, plus refresh once daily at 01:00 America/Denver.

The planned caps are 25 new source trades per wallet/run, 125 across the cohort/run, 125 Gamma enrichments/run, 500 collection/enrichment operations daily, 104 refresh operations daily for this frozen baseline, and 604 planned provider operations daily. These are planning ceilings, not a claim that every HTTP request is presently persisted or perfectly countable.

## Immutable manifest vs mutable state

`manifest_<observation_id>.json` is versioned, deterministic, and immutable. It contains the cohort, policy, cadence, caps, DB path, baseline SHA, operational lock path, and the explicit statement that creation does not activate anything.

`state_<observation_id>.json` is separate and hash-binds itself to the canonical manifest SHA-256. It records lifecycle timestamps, stop request/confirmation, explicit extension authorization, checkpoints, and the latest control verdict. `current.json` is only a convenience locator; it cannot activate a job and is never authoritative over manifest/state validation.

## Lifecycle

```
planned --(explicit future control transition)--> active
active --(request-stop)--> stop_requested --(future shutdown confirmation)--> stopped
active --(authorized end and invariants)--> completed
```

`failed_closed` is a denial state for corrupt/unsafe control artifacts. No states describe scoring, qualification, specialist approval, or execution. Completion only says that the bounded window ended; it never says a wallet qualified.

## Activation gate

Future collection and refresh wrappers must call `may_run_observation_job(manifest, state, now, job_type)`. It returns deterministic `allowed`, `reason_code`, and explanation. It permits only a validated, hash-bound `active` state inside the original or separately authorized extension window. Planned manifests, pointers, stop requests, terminal states, corrupt artifacts, unsupported jobs, and time outside the window deny work.

This milestone does **not** wire that gate into any runtime unit.

## Stop, checkpoints, and extension

A stop request is an atomic control signal only. It records a defined reason category, reason, timestamp, and optional external evidence. It neither calls systemd nor touches a DB. Identical requests are idempotent; conflicting repeats fail closed. A future wrapper may confirm `stopped` after it has actually ceased work.

Checkpoints can only be recorded while active, on/after a manifest-defined due time, once per day. They retain optional externally supplied report path, SHA-256, and operator note without fabricating findings.

An extension is a single explicit active-state transition, one through seven days, with timestamp and reason. It never changes the manifest's original planned end. It cannot occur after completion and is not automatic.

## Atomicity and locks

Every JSON write serializes deterministically, writes a same-directory temporary file, flushes and fsyncs it, atomically replaces the target, and fsyncs the containing directory. A failure before replacement leaves the prior target intact; a directory-fsync failure after replacement raises `DurabilityConfirmationError` rather than falsely reporting success. Artifact names use a strict lowercase identifier grammar, are direct children of the resolved artifact directory, and reject symlink/non-regular read or write targets.

Mutable transitions read and validate state, verify the hash and expected state under `/tmp/polycopy-specialist-observation-control.lock`, then atomically write the next state. The dedicated lock is opened with `O_NOFOLLOW`, must be an effective-user-owned regular file with exact `0600` mode, and is released by unlocking/closing its descriptor without unlinking its path. It is intentionally separate from `/tmp/polycopy-operational-jobs.lock`, which remains the future collection/refresh operational lock.

The CLI defaults its data directory from the repository root, not the caller's current directory, and reports validation, lock, and durability failures as control-plane JSON errors. Status exposes the oldest unrecorded checkpoint as `upcoming`, `due`, or `overdue`, plus the full checkpoint schedule; these are scheduling facts, not findings.

## Non-goals and future integration

This milestone does not activate production work. It does not create systemd units; inspect services/timers/journals; access the DB; poll provider, disk, memory, or health; run collection, enrichment, refresh, scoring, monitoring, execution, or notifications; or automatically stop anything.

Future milestones may integrate the pure gate into the existing collector and PR #90 refresh safety wrapper, build a read-only monitor that records externally evidenced stop categories, and separately authorize runtime/systemd installation.
