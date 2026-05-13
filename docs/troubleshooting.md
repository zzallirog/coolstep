# Troubleshooting

> Every warning `coolstep compat` can print, what it means, and the
> exact fix per distro. If you don't see your situation here, open a
> [GitHub Discussion](https://github.com/zzallirog/coolstep/discussions)
> or [file an issue](https://github.com/zzallirog/coolstep/issues/new/choose).

> **TL;DR for the impatient:** run `coolstep compat --install-plan`.
> It already prints the per-distro install commands for every gap
> detected on your host. The sections below are for cases where the
> install plan isn't enough or doesn't apply.

## Install / runtime

### Minimal host without `pip` or `pipx` (Proxmox VE base, container images)

Stripped-down hosts — Proxmox VE base, some minimal LXC images, Alpine
base — ship Python without `pip`, `pipx`, or `python3-venv`. Either
get the prerequisites (one-line, requires sudo *once*):

| Distro family | One-time prerequisite |
|---|---|
| Debian / Ubuntu / Proxmox | `sudo apt install python3-pip python3-venv pipx` |
| Fedora | `sudo dnf install python3-pip pipx` |
| Arch (already complete) | — |
| Alpine | `sudo apk add py3-pip py3-virtualenv pipx` |

…or, for a truly sudo-free path, use [`uv`](https://github.com/astral-sh/uv)
which installs as a single binary into `~/.local/bin/`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv tool install git+https://github.com/zzallirog/coolstep
```

`uv tool install` is the pipx-equivalent — isolated venv per tool,
shim in `~/.local/bin/coolstep`, fully reversible
(`uv tool uninstall coolstep`).

For Proxmox hypervisor hosts specifically: monitoring the hypervisor
itself is a server-target use case (no compositor, IPMI/Redfish often
available). If you'd rather install in a guest LXC instead of on the
PVE host, that's also valid — the daemon doesn't need to be on the
metal.

### `coolstep: command not found`

After install, the binary lands in `~/.local/bin/`. Some shells don't
have that directory on `PATH` by default.

**Fix.** Add it to `PATH` and re-source your shell config:

```bash
# fish
fish_add_path ~/.local/bin

# bash / zsh — append to .bashrc or .zshrc:
export PATH="$HOME/.local/bin:$PATH"
```

Verify: `which coolstep` should print `~/.local/bin/coolstep`.

### `pipx` warns "File exists at ~/.local/bin/coolstep"

You have both a `pip --user` install and a `pipx` install fighting for
the same shim names. Pick one path.

**Keep pipx, remove pip --user:**

```bash
pip uninstall --break-system-packages coolstep
rm -f ~/.local/bin/coolstep ~/.local/bin/coolstep-collector ~/.local/bin/coolstep-dashboard
pipx install git+https://github.com/zzallirog/coolstep
```

**Keep pip --user, remove pipx:**

```bash
pipx uninstall coolstep
# pip --user binaries already live in ~/.local/bin — nothing else needed
```

### `pip install` refuses with "externally-managed-environment"

PEP 668. Modern Arch / Debian / Fedora ship system Python that won't
let `pip` mutate system packages without an explicit opt-out.

**Fix.** Either use pipx (preferred) or pass the override:

```bash
pip install --user --break-system-packages git+https://github.com/zzallirog/coolstep
```

`--user` keeps the install in `~/.local/` — system packages are not
touched, only the protection check is bypassed.

### `makepkg`: target not found `python-uvicorn`

The Arch package for uvicorn is just `uvicorn`, not `python-uvicorn`.
Update your local repo first:

```bash
sudo pacman -Syu
```

If you still see the error, you're on an older PKGBUILD. Pull the
latest from the repo and retry.

## Sensors and signals

### `NVIDIA GPU detected but pynvml not importable`

The NVIDIA management library Python binding isn't available in the
Python environment running coolstep.

| Path | Fix |
|---|---|
| pipx install | `pipx inject coolstep nvidia-ml-py` |
| pip --user | `pip install --user --break-system-packages nvidia-ml-py` |
| Arch system Python | `sudo pacman -S python-pynvml` |
| Debian / Ubuntu | `sudo apt install python3-pynvml` |
| Fedora | `sudo dnf install python3-nvidia-ml` |

After install, re-run `coolstep compat` — the warning should be gone
and `nvidia_nvml` should appear in `Collectors expected`.

### `RAPL energy counters present but unreadable (CVE-2020-8694)`

The kernel restricts `/sys/class/powercap/intel-rapl/.../energy_uj`
reads to root after CVE-2020-8694. coolstep needs them to compute
actual CPU package power.

**Option A — `setcap` (least disruptive).** Grant the Python
interpreter the capability to read RAPL files:

```bash
sudo setcap cap_sys_admin+ep $(realpath $(which python3))
```

This sticks until Python is upgraded; re-run after every Python
version bump. The capability is bounded — Python can read RAPL but
can't, say, mount filesystems or load kernel modules unless your code
asks it to.

**Option B — systemd unit override (cleaner for the daemon).**

```bash
mkdir -p ~/.config/systemd/user/coolstep-collector.service.d
cat > ~/.config/systemd/user/coolstep-collector.service.d/20-rapl.conf <<EOF
[Service]
AmbientCapabilities=CAP_SYS_ADMIN
NoNewPrivileges=false
EOF
systemctl --user daemon-reload
systemctl --user restart coolstep-collector
```

This grants the capability only to the daemon, not to the Python
binary at large.

**Option C — accept the gap.** RAPL is a nice-to-have, not a must.
coolstep falls back to the `work_per_degree` proxy (see
[`efficiency-curve.md`](efficiency-curve.md)) which works without
package power.

### `perf_event_paranoid > 2 (PMU counters locked down)`

Hardened distros pin `kernel.perf_event_paranoid = 3`, which blocks
unprivileged `perf stat`. coolstep needs ≤ 2 to read cycles,
instructions, cache-misses.

**Fix.**

```bash
# Transient (until reboot):
echo 2 | sudo tee /proc/sys/kernel/perf_event_paranoid

# Persistent:
echo 'kernel.perf_event_paranoid = 2' | sudo tee /etc/sysctl.d/60-perf.conf
sudo sysctl --system
```

On modern Linux kernels (≥ 5.8), even `paranoid = 2` is not enough for
`perf stat -a` (system-wide). The full fix:

```bash
# Either set paranoid = 0 (more permissive)
echo 0 | sudo tee /proc/sys/kernel/perf_event_paranoid

# Or grant CAP_PERFMON to the daemon (see RAPL Option B template,
# replace CAP_SYS_ADMIN with CAP_PERFMON)
```

### `asusctl: False` on an ASUS host

`asusctl` is the canonical ASUS fan-curve and platform-profile actuator.
coolstep wraps it; without it, the `asusctl_fan_curve_bias` actuator
doesn't activate.

| Distro | Command |
|---|---|
| Arch | `yay -S asusctl rog-control-center` (AUR) |
| Fedora | `sudo dnf copr enable lukenukem/asus-linux && sudo dnf install asusctl` |
| openSUSE | `sudo zypper install asusctl` (may need Packman repo) |
| Debian / Ubuntu | Build from source: <https://gitlab.com/asus-linux/asusctl> |

After install: `asusctl --help` should respond. coolstep recognizes
v6+ (older versions had different CLI shapes).

### `ryzenadj: False` on an AMD host

`ryzenadj` is the AMD STAPM / fast-PPT / slow-PPT tuner. coolstep
wraps it for the `ryzenadj_cap` actuator.

| Distro | Command |
|---|---|
| Arch | `yay -S ryzenadj` (AUR) |
| Debian | `sudo apt install build-essential cmake libpci-dev` then build from <https://github.com/FlyGoat/RyzenAdj> |
| Fedora | `sudo dnf install gcc cmake pciutils-devel` then build from source |

### `bpftrace: False` (eBPF sched events unavailable)

bpftrace gives 10–50 ms head-start on workload detection via scheduler
tracepoints. Without it, coolstep falls back to `/proc/stat` polling
(slower).

| Distro | Command |
|---|---|
| Arch | `sudo pacman -S bpftrace` |
| Debian / Ubuntu | `sudo apt install bpftrace` |
| Fedora | `sudo dnf install bpftrace` |
| openSUSE | `sudo zypper install bpftrace` |

After install, the daemon needs `CAP_PERFMON` (see the systemd unit
override template above with `CAP_PERFMON` in place of
`CAP_SYS_ADMIN`).

### `ipmitool: False` on a server host

`ipmitool` is the canonical IPMI client for BMC-managed servers.
Without it, the `ipmi` collector can't read chassis fans, inlet temps
or PSU power.

| Distro | Command |
|---|---|
| Arch | `sudo pacman -S ipmitool` |
| Debian / Ubuntu | `sudo apt install ipmitool` |
| Fedora | `sudo dnf install ipmitool freeipmi` |
| openSUSE | `sudo zypper install ipmitool` |

For BMC access from a separate host, also set:

```bash
export COOLSTEP_IPMI_HOST=10.0.0.1
export COOLSTEP_IPMI_USER=ADMIN
export COOLSTEP_IPMI_PASS=<your bmc password>
```

For local KCS access (when coolstep runs on the same OS as the BMC),
make sure `ipmi_si` or `ipmi_devintf` kernel modules are loaded:

```bash
sudo modprobe ipmi_devintf
sudo modprobe ipmi_si
```

### `no COOLSTEP_REDFISH_URL env (set for rack-server BMC)`

Redfish is the modern JSON-over-HTTPS replacement for IPMI on
enterprise hardware (iDRAC, iLO, Supermicro, XClarity).

```bash
export COOLSTEP_REDFISH_URL=https://<bmc-ip>
export COOLSTEP_REDFISH_USER=admin
export COOLSTEP_REDFISH_PASS=<your bmc password>
export COOLSTEP_REDFISH_VERIFY_TLS=0   # default 1; set 0 for self-signed
```

Put these in the systemd unit drop-in for persistence:

```bash
mkdir -p ~/.config/systemd/user/coolstep-collector.service.d
cat > ~/.config/systemd/user/coolstep-collector.service.d/30-redfish.conf <<EOF
[Service]
Environment=COOLSTEP_REDFISH_URL=https://10.0.0.1
Environment=COOLSTEP_REDFISH_USER=admin
Environment=COOLSTEP_REDFISH_PASS=secret
EOF
systemctl --user daemon-reload
systemctl --user restart coolstep-collector
```

## Calibration gates

### `hyprctl_consistency` is failing

The workload-context collector is dropping more than 5% of samples,
usually because of a session-startup race where the systemd user
manager started before Hyprland exported its environment.

**Fix on Hyprland:** add the env import to your Hyprland startup.

```conf
# ~/.config/hypr/hyprland.conf (or wherever you keep autostart)
exec-once = systemctl --user import-environment HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
exec-once = systemctl --user restart coolstep-collector coolstep-dashboard
```

The restart guarantees the daemon picks up the now-propagated env. Log
out and back in to make it permanent.

**If you're not on Hyprland:** this gate doesn't apply — your
`dbus_session` collector (KDE / GNOME via `gdbus`) handles workload
context. Verify with `coolstep adapters` that `dbus_session` is in the
discovered list.

### `class_diversity` is stuck below 3

The workload classifier wants at least three distinct classes (CODE /
GAME / RENDER / IDLE / OTHER) during the calibration window.

If you've only used one kind of workload during calibration (e.g., the
host has been browsing-only for 24 hours), the gate stays stuck. The
fix is more diverse usage — run a build, play a game, idle for an
hour, repeat.

For server hosts where this diversity is unrealistic, lower the
threshold:

```bash
echo 'COOLSTEP_WORKLOAD_CLUSTERS=2' >> ~/.config/coolstep/env
```

### `throttle_events` is stuck at 0

The host has never been hot enough to trigger the FSM (90 °C enter,
85 °C exit, 3 s minimum). Either:

- The host genuinely never throttles (well-cooled, undervolted, or
  server-class) — lower the threshold to 0:

  ```bash
  echo 'COOLSTEP_THROTTLE_EVENTS=0' >> ~/.config/coolstep/env
  ```

  The predictor will run on the trajectory-fallback signal only.

- The throttle FSM is wrong for your host (some chips report different
  sensor names). Check `coolstep tail -n 5` and verify `cpu_temp` is
  populated. If it's zero or `None`, your hwmon driver isn't being
  recognized — see the next section.

### hwmon driver missing — `super-I/O: none` on a desktop

You're on a desktop (probably ASUS, MSI, Gigabyte, ASRock) and
coolstep doesn't see your fan RPM sensors. Likely you need the
`nct6687-driver-dkms-git` (or similar) kernel module:

| Vendor | Module |
|---|---|
| MSI / Gigabyte / ASRock modern boards | `nct6687-driver-dkms-git` (AUR) or build from <https://github.com/Fred78290/nct6687d> |
| Older ASUS / MSI | `nct6775-dkms` or built-in `nct6775` |
| ASUS ROG (some models) | `asus-wmi-sensors-dkms-git` (AUR) |
| ASUS recent (B650, X670) | `asus-ec-sensors` (in-tree, just needs `modprobe`) |

After install: `sudo modprobe <module>`, then `coolstep compat` should
show the driver name under `super-I/O:`.

## Daemon

### Dashboard at `http://127.0.0.1:18889/` doesn't respond

```bash
systemctl --user status coolstep-dashboard
```

If "inactive (dead)" — start it:

```bash
systemctl --user start coolstep-dashboard
```

If "failed" — read the log:

```bash
journalctl --user -u coolstep-dashboard -n 50
```

Most common failures are: missing Python dep (`uvicorn`, `fastapi`),
port 18889 already in use, or insufficient `MemoryMax` (we ship
768 MB; if you reduced it, raise it back).

### Dashboard shows "SSE disconnected — auto-reconnect"

Usually `MemoryMax` too tight. Open
`~/.config/systemd/user/coolstep-dashboard.service.d/10-memory.conf`
and ensure:

```ini
[Service]
MemoryMax=768M
# do NOT set MemoryHigh — it breaks SSE long-poll
```

`daemon-reload && restart`. The browser's `EventSource` reconnects
automatically.

### `chromadb` segfaults on Python 3.14

Known issue with chromadb-rust-bindings on Python 3.14.

**Preferred fix (v0.5.9+) — switch the KNN backend to HNSW:**

```bash
echo 'COOLSTEP_KNN_BACKEND=hnsw' >> ~/.config/coolstep/env
systemctl --user restart coolstep-collector
```

The HNSW path uses `chroma-hnswlib` directly and never imports the
crashing rust bindings. Full KNN prediction is preserved (neighbours,
`suggested_rpm`, `danger_neighbour_count` all keep working). See
ADR-022 for the rationale and `scripts/cutover_to_hnsw.sh` for a
guided migration on an existing host.

**Emergency fallback — disable ChromaDB entirely:**

```bash
echo 'COOLSTEP_CHROMA_DISABLED=1' >> ~/.config/coolstep/env
systemctl --user restart coolstep-collector
```

The predictor falls back to `MetaPredictor(TrajectoryBaseline +
ResidualBank)` — Newton-cooling forecast plus bucketed residual
correction stays active; only the KNN neighbour signals go quiet.

### `chroma-hnswlib` vs upstream `hnswlib` — silent KNN empty-results

If you ever `pip install hnswlib` (the upstream package) into the same
venv as coolstep, the upstream `.so` silently overwrites
`chroma-hnswlib`'s vendored `.so`. Symptoms: KNN queries return zero
neighbours, no errors logged, dashboard `neighbours_tile` empty.

Fix:

```bash
pip uninstall -y hnswlib chroma-hnswlib
pip install "chroma-hnswlib>=0.7.6"
```

`pyproject.toml` `[ml]` extras pin `chroma-hnswlib>=0.7.6` and do not
list bare `hnswlib`. Do not install both side-by-side.

### HNSW swap failed / `data/hnsw.backup/` is present

The `embedder_refit.refit_and_swap` pipeline (drift-triggered) does an
atomic-ish rename: `data/hnsw/` → `data/hnsw.backup/`, then
`data/hnsw.staging/` → `data/hnsw/`. On clean shutdown the backup
remains as a safety copy and is overwritten on the next refit.

If you see `embedder_refit: post_swap_discover_failed` in the journal,
the live dir is bound to an unreadable index. Recovery:

```bash
systemctl --user stop coolstep-collector
rm -rf ~/coolstep/data/hnsw           # remove the failed swap
mv ~/coolstep/data/hnsw.backup ~/coolstep/data/hnsw
systemctl --user start coolstep-collector
```

`scripts/hnsw_rollback.sh --restore-data` automates the same recovery
from any `data/hnsw.bak-<stamp>/` created by `cutover_to_hnsw.sh`.

## Still stuck?

- Run `coolstep doctor` — full health check with verdicts
- Run `coolstep compat --install-plan` — exact install commands
- Open a [GitHub Discussion](https://github.com/zzallirog/coolstep/discussions)
  with the output of both
- [File an issue](https://github.com/zzallirog/coolstep/issues/new/choose)
  with the bug-report template

Include `coolstep compat --json` output when reporting — it's the
single most useful artifact for triage.
