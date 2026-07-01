# Polycopy automation service templates with OnFailure= hook

These templates show the **service** unit shape for the 5 paper-pilot
automation services. They are templates — do NOT install them as-is.
The live unit files live at `/etc/systemd/system/` and were created
during earlier pilot setup.

## Important: the OnFailure= hook in these templates is NOT yet wired into the live units

The current `/etc/systemd/system/polycopy-{collect,scan,settle,update,health}.service`
files do **not** yet contain `OnFailure=polycopy-pilot-report.service`. They were
deployed before the pilot-monitoring PR was prepared, and adding the hook to the
live units is a **separately approved deployment step** that is **not** part of
PR #12.

The templates below document the recommended shape for that future step.

## Why `OnFailure=` belongs on the service unit, not the timer

A `oneshot` service does not cause its timer unit to enter a `failed` state
— the timer is just the schedule. The service itself enters `failed` when
its process exits non-zero. Putting `OnFailure=` on the timer would never
fire in practice. So the hook belongs on the **service** units listed below.

## Recommended template changes for a future deployment

The next time an operator updates the 5 automation service units in a
separately approved step, the change is to add this line to the `[Unit]`
section of each:

```ini
OnFailure=polycopy-pilot-report.service
```

Effect: any of those services entering `failed` state immediately triggers
a `polycopy-pilot-report.service` run, which classifies the resulting
report (typically YELLOW or RED), refreshes the latest-report file, and
records the event in the journal.

---

## polycopy-collect.service

```ini
[Unit]
Description=Polycopy smart-money data collection
After=network-online.target polycopy-api.service
Wants=network-online.target
OnFailure=polycopy-pilot-report.service

[Service]
Type=oneshot
WorkingDirectory=/root/Polycopy
EnvironmentFile=/root/Polycopy/.env
ExecStart=/root/mlb-ev-model-lab/.venv/bin/python3 /root/Polycopy/scripts/collect_smart_money_data.py --limit 50
TimeoutStartSec=900
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal
```

---

## polycopy-scan.service

```ini
[Unit]
Description=Polycopy smart-money scan + scoring
After=network-online.target polycopy-api.service polycopy-collect.service
Wants=network-online.target
OnFailure=polycopy-pilot-report.service

[Service]
Type=oneshot
WorkingDirectory=/root/Polycopy
EnvironmentFile=/root/Polycopy/.env
ExecStart=/root/mlb-ev-model-lab/.venv/bin/python3 /root/Polycopy/scripts/run_scan.py --market-limit 20
TimeoutStartSec=900
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal
```

---

## polycopy-settle.service

```ini
[Unit]
Description=Polycopy paper position settlement
After=network-online.target polycopy-api.service
Wants=network-online.target
OnFailure=polycopy-pilot-report.service

[Service]
Type=oneshot
WorkingDirectory=/root/Polycopy
EnvironmentFile=/root/Polycopy/.env
ExecStart=/root/mlb-ev-model-lab/.venv/bin/python3 /root/Polycopy/scripts/settle_paper_positions.py
TimeoutStartSec=900
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal
```

---

## polycopy-update.service

```ini
[Unit]
Description=Polycopy paper portfolio mark-to-market update
After=network-online.target polycopy-api.service polycopy-settle.service
Wants=network-online.target
OnFailure=polycopy-pilot-report.service

[Service]
Type=oneshot
WorkingDirectory=/root/Polycopy
EnvironmentFile=/root/Polycopy/.env
ExecStart=/root/mlb-ev-model-lab/.venv/bin/python3 /root/Polycopy/scripts/update_paper_portfolio.py
TimeoutStartSec=900
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal
```

---

## polycopy-health.service

```ini
[Unit]
Description=Polycopy non-destructive ingestion health check
After=network-online.target polycopy-api.service
Wants=network-online.target
OnFailure=polycopy-pilot-report.service

[Service]
Type=oneshot
WorkingDirectory=/root/Polycopy
EnvironmentFile=/root/Polycopy/.env
ExecStart=/root/mlb-ev-model-lab/.venv/bin/python3 /root/Polycopy/scripts/post_merge_ingestion_health_check.py
TimeoutStartSec=120
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal
```
