# Privileges & root — what coolstep needs, and why

> coolstep runs as your regular user. The daemon does *not* require
> root, and you should *never* install or run it via `sudo`. But the
> hardware writes — PWM curves, RAPL counters, CPU boost limits — do
> need privileged access at the OS level. This document explains exactly
> where, why, and how to grant the minimum needed.

## TL;DR

| What | Needs root? | Recommended path |
|---|---|---|
| `coolstep compat` (read-only) | No | Just run it. |
| `coolstep-dashboard` (web UI) | No | systemd user service. |
| `coolstep-collector` (telemetry sampling) | No | systemd user service. |
| `notify_send` actuator (desktop popup) | No | — |
| `readonly_log` actuator (audit) | No | — |
| `asusctl_fan_curve_bias` (ASUS fan curve write) | Yes — `asusctl` | polkit rule (asusctl ships one) |
| `epp_shift` (EPP write to `/sys/.../cpufreq/`) | Yes — sysfs root | systemd drop-in with `AmbientCapabilities=CAP_SYS_ADMIN` |
| `ryzenadj_cap` (AMD STAPM/PPT via MSR) | Yes — MSR | sudoers NOPASSWD for `/usr/bin/ryzenadj` |
| `rapl_energy` (read `/sys/.../energy_uj`) | Yes — CVE-2020-8694 | `setcap cap_sys_admin+ep` on Python, or `AmbientCapabilities` |
| `ebpf_sched` (`bpftrace`) | Yes — `CAP_PERFMON` | systemd drop-in with `AmbientCapabilities=CAP_PERFMON` |
| `perf_events` (system-wide `perf stat -a`) | Yes — `CAP_PERFMON` | same as eBPF |

## The two principles

1. **Never install coolstep with `sudo`.** Use `pipx` or `pip install
   --user`. Anything else either misplaces files or pollutes system
   Python.
2. **Grant the minimum capability the daemon actually needs, scoped to
   the daemon, not to your whole user account.** Everything below
   follows that rule.

## Read-only mode requires nothing

If you just want the dashboard and the platform report, you need *no*
privileges at all. `coolstep compat` reads sysfs files that are
world-readable. The dashboard reads SQLite written by your own user.
You can stop here and never touch root.

This is the recommended mode for the first 7+ days while the predictor
calibrates against your actual workload.

## Read-only sensors that *do* want a capability

A few sensors are root-only because of kernel CVE mitigations:

### RAPL energy counters

`/sys/class/powercap/intel-rapl/.../energy_uj` is restricted to root
after [CVE-2020-8694](https://nvd.nist.gov/vuln/detail/CVE-2020-8694).
Without it, the `rapl_energy` collector still discovers but yields no
values; `coolstep compat` shows it under warnings.

**Granular fix — drop-in on the daemon unit:**

```bash
mkdir -p ~/.config/systemd/user/coolstep-collector.service.d
cat > ~/.config/systemd/user/coolstep-collector.service.d/20-rapl.conf <<'EOF'
[Service]
AmbientCapabilities=CAP_SYS_ADMIN
NoNewPrivileges=false
EOF
systemctl --user daemon-reload
systemctl --user restart coolstep-collector
```

The capability is bounded — only `coolstep-collector` gets it, only
while it runs. No other process under your user is affected. The
daemon does not call out to shells or other binaries with this
capability, so the exposure is contained to coolstep's own code.

### perf PMU counters

`perf stat -a` (system-wide) needs `CAP_PERFMON` on modern kernels
(≥ 5.8), regardless of the `perf_event_paranoid` setting.

```bash
cat > ~/.config/systemd/user/coolstep-collector.service.d/30-perfmon.conf <<'EOF'
[Service]
AmbientCapabilities=CAP_PERFMON
NoNewPrivileges=false
EOF
```

### bpftrace scheduler events

Same as `perf` — `CAP_PERFMON` (or `CAP_SYS_ADMIN` on kernels < 5.8).
The same drop-in covers both.

## Hardware writes — three actuators, three privilege paths

This is the part the user must explicitly opt into. coolstep ships
with `COOLSTEP_ACTUATOR_ENABLE=false` by default — hardware writes
won't fire even if you've granted the capabilities below. You also
need to flip that env var (see [calibration-gates.md](calibration-gates.md))
*and* the eight calibration gates must clear.

### `asusctl_fan_curve_bias` — ASUS fan curve writes

`asusctl` is the canonical ASUS tool. It exposes a DBus interface and
ships its own polkit rules — you authenticate once at install time
(`asusctl` brings up a polkit prompt the first time), and after that
the user can manage their own fan curves without sudo.

If you don't see the polkit prompt, the package didn't install its
rule; copy the rule yourself:

```bash
# Verify the rule is installed:
ls /usr/share/polkit-1/rules.d/ | grep asusctl

# If missing, asusctl ships a sample at:
# /usr/share/asusctl/policies/
# Copy or symlink it into /usr/share/polkit-1/rules.d/
```

No sudoers entry needed. coolstep just calls `asusctl fan-curve ...`;
asusctl handles the privilege escalation.

### `epp_shift` — Energy Performance Preference writes

EPP lives at `/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference`.
Owned by `root:root`, mode 0644 by default — readable by anyone,
writable only by root.

**Recommended path:** systemd drop-in with `CAP_SYS_ADMIN` (same
drop-in as RAPL above). One capability, two read/write needs.

**Less-recommended alternative:** `chmod g+w` on the EPP files plus a
udev rule. Persists across reboots but spreads write permission
beyond coolstep.

### `ryzenadj_cap` — AMD STAPM / fast-PPT / slow-PPT writes

`ryzenadj` writes to MSRs (model-specific registers) on the CPU. MSR
access is root-only, period. There is no polkit rule for ryzenadj.

The clean path is a **per-command sudoers drop-in**:

```bash
sudo tee /etc/sudoers.d/coolstep-ryzenadj <<EOF
# Allow $USER to run ryzenadj without password — required for
# coolstep_cap_boost actuator. ryzenadj writes to MSRs only.
$USER ALL=(root) NOPASSWD: /usr/bin/ryzenadj
EOF
sudo chmod 0440 /etc/sudoers.d/coolstep-ryzenadj
# visudo-style validation:
sudo visudo -c -f /etc/sudoers.d/coolstep-ryzenadj
```

Then update `~/.config/systemd/user/coolstep-collector.service.d/40-ryzenadj.conf`:

```ini
[Service]
Environment=COOLSTEP_RYZENADJ_CMD="sudo /usr/bin/ryzenadj"
```

The daemon will prepend `sudo` to ryzenadj invocations. Because of
NOPASSWD, no password prompt — but the audit trail still lands in
`/var/log/auth.log` for every write.

**Why sudoers and not setcap?** ryzenadj needs to open `/dev/cpu/*/msr`,
which is mode `0600 root:root`. `setcap CAP_SYS_RAWIO` on the ryzenadj
binary works but it's coarser — any user who can `exec()` ryzenadj
gets that capability. Sudoers scopes it to your user.

### What about `ec_sys` direct register writes?

coolstep does not write to `ec_sys` directly (ADR-010). It wraps
vendor tools that know which registers are safe. If you want
ec_sys-level control, run a vendor-specific community tool
(`nbfc-linux`, `coolercontrol`) alongside coolstep — coolstep will
defer to whichever tool actually owns the fan PWMs.

## The full setup — recommended order

This is the script-ready ordering for someone who wants the full
hardware-writes stack on a typical ASUS laptop with AMD CPU:

```bash
# 0. Install coolstep — USER-LEVEL, no sudo
pipx install git+https://github.com/zzallirog/coolstep

# 1. Run read-only for ≥ 7 days. Calibrate. Watch the dashboard.
systemctl --user enable --now coolstep-collector coolstep-dashboard
xdg-open http://127.0.0.1:18889/

# 2. When 8/8 calibration gates are green, grant capabilities.
#    Sensor reads first — these don't actuate anything:
mkdir -p ~/.config/systemd/user/coolstep-collector.service.d
cat > ~/.config/systemd/user/coolstep-collector.service.d/20-sensors.conf <<'EOF'
[Service]
AmbientCapabilities=CAP_SYS_ADMIN CAP_PERFMON
NoNewPrivileges=false
EOF

# 3. Hardware writes — separate file, separate decision:
#    asusctl already works via its own polkit rules.
#    ryzenadj needs a sudoers entry:
sudo tee /etc/sudoers.d/coolstep-ryzenadj > /dev/null <<EOF
$USER ALL=(root) NOPASSWD: /usr/bin/ryzenadj
EOF
sudo chmod 0440 /etc/sudoers.d/coolstep-ryzenadj

cat > ~/.config/systemd/user/coolstep-collector.service.d/40-actuators.conf <<'EOF'
[Service]
Environment=COOLSTEP_ACTUATOR_ENABLE=true
Environment=COOLSTEP_RYZENADJ_CMD="sudo /usr/bin/ryzenadj"
EOF

# 4. Reload and restart
systemctl --user daemon-reload
systemctl --user restart coolstep-collector

# 5. Verify
coolstep compat
journalctl --user -u coolstep-collector -n 20
```

## What if I never want hardware writes?

You can run coolstep forever in monitoring-only mode. Skip steps 2 and
3 above. The predictor still runs, the dashboard still updates, the
read-only metrics still tell you what your machine is doing. Many
users prefer this — coolstep as a thermal observability tool, not as
a fan controller.

## Why this is more honest than "we use root for safe operations"

Other "thermal management" software often installs a system-wide
daemon that runs as root with full access, hides what it actually
writes, and bundles its own polkit/dbus layer that you can't audit.
coolstep refuses that pattern. The daemon runs as you, the
capabilities are granted in files you can read, the actions land in
`actuator-journal.jsonl` that you own, and you can revoke any of it
with a single `rm`.

The price of that honesty is the setup section above. It's longer
than `sudo apt install` but every line is auditable, every privilege
is scoped, and nothing is hidden.

## See also

- [calibration-gates.md](calibration-gates.md) — the eight checks
  between dry-run and armed mode
- [troubleshooting.md](troubleshooting.md) — common warnings about
  missing capabilities and how to fix them
- [stack-decisions.md](stack-decisions.md) — ADR-010 records the
  decision to wrap vendor tools instead of writing to hwmon directly
