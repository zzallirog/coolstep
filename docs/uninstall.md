# Uninstall

> Coolstep is a user-level tool with no system writes. Uninstall
> touches `$HOME` only. `pipx uninstall` / `pip uninstall` remove the
> package + binaries, but **not** runtime state and **not** the systemd
> unit files — `coolstep install-units` drops the units in
> `~/.config/systemd/user/`, and there is no reverse command yet.

## Full purge

```bash
# 1. Stop + disable the daemon units
systemctl --user disable --now coolstep-collector coolstep-dashboard

# 2. Drop unit files + drop-ins + enable symlinks
rm -rf ~/.config/systemd/user/coolstep-collector.service \
       ~/.config/systemd/user/coolstep-dashboard.service \
       ~/.config/systemd/user/coolstep-collector.service.d/ \
       ~/.config/systemd/user/coolstep-dashboard.service.d/ \
       ~/.config/systemd/user/default.target.wants/coolstep-*.service
systemctl --user daemon-reload

# 3. Uninstall the Python package
pipx uninstall coolstep                                    # Path A
pip uninstall --user --break-system-packages coolstep      # Path B

# 4. Drop runtime state (up to ~2 GB after a long calibration window)
rm -rf ~/coolstep/data/   # = $COOLSTEP_HOME default

# 5. Optional: vacuum journals
journalctl --user --vacuum-size=10M
```

## What is NOT cleaned up automatically

`pipx uninstall` / `pip uninstall` remove the installed package +
binaries under `~/.local/bin/`. They leave behind:

- `~/coolstep/data/` (`COOLSTEP_HOME`) — runtime state, up to ~2 GB
- `~/.config/systemd/user/coolstep-*.service` — unit files
- `~/.config/systemd/user/coolstep-*.service.d/*.conf` — drop-ins
  (e.g. `60-chroma-disabled.conf`, `dev.conf`)
- `~/.config/systemd/user/default.target.wants/coolstep-*.service` —
  enable symlinks
- Journal entries (`journalctl --user -u coolstep-*`)

## Verification

```bash
which coolstep                                         # → empty
ls ~/.config/systemd/user/coolstep-* 2>/dev/null       # → empty
ls ~/coolstep/data/ 2>/dev/null                        # → empty
systemctl --user list-units --all | grep coolstep    # → empty
```

## Saving data before removal

```bash
# Export readable telemetry
coolstep export-telemetry --since=30d --out=~/coolstep-export.jsonl

# Or a full tarball of the runtime state
tar czf ~/coolstep-data-backup.tgz -C ~ coolstep/data/
```
