# deploy-units/

Systemd unit templates for the paper-pilot automation and monitoring
stack. These are **templates**, not deployable unit files — they live in
the repo for review, version control, and reproducible deployment.

## Layout

- `polycopy-pilot-report.service.template`
- `polycopy-pilot-report-morning.timer.template`
- `polycopy-pilot-report-evening.timer.template`
- `automation-services.template.md` — templates for the 5 existing
  automation service units (collect, scan, settle, update, health) with
  `OnFailure=polycopy-pilot-report.service` correctly placed on each
  service unit (not the timer).

## How to use

To install on a fresh host, rename each `.template` to drop that suffix
and copy into `/etc/systemd/system/`, then `systemctl daemon-reload` and
`systemctl enable --now`.

Example:

```bash
sudo install -m 644 deploy-units/polycopy-pilot-report.service.template \
    /etc/systemd/system/polycopy-pilot-report.service
sudo install -m 644 deploy-units/polycopy-pilot-report-morning.timer.template \
    /etc/systemd/system/polycopy-pilot-report-morning.timer
sudo install -m 644 deploy-units/polycopy-pilot-report-evening.timer.template \
    /etc/systemd/system/polycopy-pilot-report-evening.timer
sudo systemctl daemon-reload
sudo systemctl enable --now polycopy-pilot-report-morning.timer
sudo systemctl enable --now polycopy-pilot-report-evening.timer
```

The automation service templates in `automation-services.template.md`
are reference material — they are already deployed on the running host
and were edited by hand during pilot setup. Re-applying them is optional;
the live units at `/etc/systemd/system/` are the source of truth.

## Why `.template` suffix?

The `.template` suffix prevents anyone from accidentally `systemctl
enable`-ing a file from this directory. Only files at
`/etc/systemd/system/` are loaded by systemd.