# Polycopy automation service templates with OnFailure= hook

These templates show the **service** unit shape for the 5 paper-pilot
automation services. They are templates — do NOT install them as-is.
The live unit files live at `/etc/systemd/system/` and were created during
PR #11's pilot-monitoring setup. They already include `OnFailure=`.

The important change vs. the pre-monitoring shape is:

```ini
[Unit]
...
OnFailure=polycopy-pilot-report.service
```

This hook attaches to the **service** unit (not the timer) because systemd
marks a `oneshot` service as `failed` when its process exits non-zero, and
a timer unit is just the schedule — it does not enter `failed` state when
the service it spawned fails. Putting `OnFailure=` on the timer would never
fire in practice.

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