# dropin-templates — systemd drop-in templates for coolstep-collector

These files are plain systemd `[Service]` drop-ins rendered by
`scripts/coolstep_init.sh` during bootstrap.  They are **templates** —
`coolstep_init.sh` copies the right one to:

    ~/.config/systemd/user/coolstep-collector.service.d/05-init-profile.conf

after substituting any `${VAR}` tokens via `envsubst`.

## Profiles

| File           | CPUQuota | MemoryMax | Actuator |
|----------------|----------|-----------|----------|
| `laptop.conf`  | 10%      | 512M      | allowed  |
| `desktop.conf` | 30%      | 1G        | allowed  |
| `server.conf`  | 50%      | 2G        | disabled |

No `Slice=` directive — the unit defaults to `user.slice` which is
always present.  Pin a custom slice in a higher-numbered drop-in if
your host uses a layered slice layout.

## Auto-detection heuristics (in `coolstep_init.sh`)

- **laptop**: `/sys/class/power_supply/BAT*` exists AND reports a
  battery-shaped state (`Discharging` / `Charging` / `Unknown`) —
  excludes desktops with a USB UPS that ACPI exposes as `BAT*`.
- **server**: no display server (`DISPLAY`/`WAYLAND_DISPLAY` empty and no
  `/tmp/.X*` sockets) **or** chassis type `server` in DMI.
- **desktop**: everything else.

## Customising

Edit the template file and re-run `scripts/coolstep_init.sh` — it will
overwrite `05-init-profile.conf` with the updated values.  Do **not**
edit `05-init-profile.conf` directly; it will be overwritten on the next
bootstrap run.  Place host-specific overrides in a higher-numbered drop-in
(e.g. `10-local.conf`).
