# Polycopy paper-pilot monitoring deployment

This document describes how to install the paper-pilot status-report script
(`scripts/paper_pilot_status.py`) and its accompanying systemd units on a
fresh host. It is **not** auto-applied — operators must review and run
the commands below after merging the PR.

## Contents of this PR

- `scripts/paper_pilot_status.py` — read-only status report (GREEN/YELLOW/RED).
- `tests/test_paper_pilot_status.py` — focused unit tests (mocked I/O).
- `.gitignore` — excludes `backups/`, `data/snapshots/`, `data/pilot_status_latest.txt`.
- `data/snapshots/.gitkeep` — preserves the directory in git while ignoring its contents.
- `docs/pilot-monitoring-deployment.md` — this file.

## What the report covers

The script inspects (all read-only):

- Local API at `http://127.0.0.1:8765/system/status`.
- Public dashboard at `https://polymoney.duckdns.org/` (via `--resolve`
  bypass for the local DNS path quirk; no remote calls except the GET itself).
- Production SQLite at `/root/Polycopy/data/polycopy.db` (opened with
  `?mode=ro` URI + `PRAGMA query_only = ON`).
- systemd state for `polycopy-{collect,scan,health,settle,update}` services
  and timers, plus `polycopy-api`, `polycopy-dashboard`, `caddy`.
- Journal history (last 12 hours) for failure detection.

It does **not** modify any service, DB, or runtime state.

## Exit codes

- `0` — GREEN (all checks pass)
- `1` — YELLOW (attention needed, safety intact)
- `2` — RED (pilot unsafe or automation failing)

## Installation

After merging the PR, on the target host (this VPS):

```bash
# 1. Place unit files (do NOT commit these to the repo — runtime config).
sudo install -m 644 deploy-units/polycopy-pilot-report.service \
    /etc/systemd/system/polycopy-pilot-report.service
sudo install -m 644 deploy-units/polycopy-pilot-report-morning.timer \
    /etc/systemd/system/polycopy-pilot-report-morning.timer
sudo install -m 644 deploy-units/polycopy-pilot-report-evening.timer \
    /etc/systemd/system/polycopy-pilot-report-evening.timer

sudo systemctl daemon-reload

# 2. Enable the two schedule timers.
sudo systemctl enable --now polycopy-pilot-report-morning.timer
sudo systemctl enable --now polycopy-pilot-report-evening.timer

# 3. Run once manually to verify.
sudo systemctl start polycopy-pilot-report.service
cat /root/Polycopy/data/pilot_status_latest.txt
```

The `deploy-units/` directory in this PR contains the unit-file templates
(see the PR diff).

## Schedule

Both timers interpret `OnCalendar` in `America/Denver` (timezone specifier
in the calendar expression itself; `Environment=TZ=` on the service is
kept for any tz-sensitive logging).

| Timer                          | OnCalendar                       | Next run (Denver) |
|--------------------------------|----------------------------------|-------------------|
| `polycopy-pilot-report-morning.timer` | `*-*-* 08:02:00 America/Denver` | 08:02 MDT/MST      |
| `polycopy-pilot-report-evening.timer` | `*-*-* 19:32:00 America/Denver` | 19:32 MDT/MST      |

### Why the 2-minute offsets

The literal 08:00:00 and 19:00:00 would collide with existing automation:

- `polycopy-collect.timer` fires at `:00` of every 15 minutes (08:00 included).
- `polycopy-settle.timer` fires at `:30` of every hour.

Offsets to `08:02` and `19:32` keep the report runs strictly separate from
collection and settlement runs. This was an explicit spec requirement:
**"Do not overlap report runs."**

## OnFailure wiring

A failed `oneshot` service does **not** cause its timer unit to enter a
`failed` state — so `OnFailure=` belongs on the **service** units, not the
timer units. Add this line to the `[Unit]` section of each of these five
service units:

- `polycopy-collect.service`
- `polycopy-scan.service`
- `polycopy-health.service`
- `polycopy-settle.service`
- `polycopy-update.service`

The line to add:

```ini
OnFailure=polycopy-pilot-report.service
```

Effect: any of those services entering `failed` state immediately triggers
a `polycopy-pilot-report.service` run, which classifies the resulting report
(typically YELLOW or RED), refreshes `/root/Polycopy/data/pilot_status_latest.txt`,
and records the event in the journal.

## Alerts

There is **no external notification channel** configured for this pilot:

- GREEN/YELLOW/RED reports write to `/root/Polycopy/data/pilot_status_latest.txt`
  (the runtime artifact excluded by `.gitignore`).
- All runs also go to journald (`journalctl -u polycopy-pilot-report.service`).
- External Telegram/email alerts are **not configured** for this pilot.
  Adding them is a separate decision that requires credentials, a notification
  integration, and explicit Todd approval.

## Manual usage

```bash
# Plain-text status (exits 0/1/2)
/root/mlb-ev-model-lab/.venv/bin/python3 \
    /root/Polycopy/scripts/paper_pilot_status.py

# JSON for tooling
... --json

# Save the latest report to data/pilot_status_latest.txt
... --write-latest

# Test classification logic without touching live state
... --mock is_live=true           # exit 2 (RED)
... --mock kill_switch=false      # exit 2 (RED)
... --mock orphan=1               # exit 2 (RED)
... --mock order=1                # exit 2 (RED)
... --mock timer=fail             # exit 1 (YELLOW, single failure)
... --mock fresh=stale            # exit 1 (YELLOW)
```

## What the script does NOT touch

- No edits to `/etc/caddy/Caddyfile`.
- No changes to DuckDNS token or hostname.
- No edits to `/root/Polycopy/.env`.
- No edits to the live `polycopy-{api,dashboard,collect,scan,health,settle,update}.service`
  unit files (only adds `OnFailure=` lines per the spec).
- No mutation of `/root/Polycopy/data/polycopy.db`.
- No order placement; no `/paper/preview` or `/paper/approve` calls.

## Validation done in this PR

- `python3 -m py_compile scripts/paper_pilot_status.py` — clean.
- 20 focused tests in `tests/test_paper_pilot_status.py` — all pass
  (against an isolated test DB).
- Full test suite against isolated test DB: 900 passed, 34 pre-existing
  failures unrelated to this PR (those failures trace to
  `POLYCOPY_ORDER_KILL_SWITCH=true` in production `.env` being read by
  tests that assert defaults — same set observed before the monitoring
  branch was created).
- Manual run on live system: GREEN, exit 0, no DB mutation
  (production DB SHA-256 unchanged across manual + systemd runs).