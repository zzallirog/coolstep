# P3 — Two deployment targets over one engine

> The design story behind the `desktop` and `server` split: same brain,
> different hands, different question being asked.

## The premise

coolstep started as a laptop daemon. The first user was a single ASUS
TUF A15, and the goal was "make the fan quieter without sacrificing
performance." That problem is real and the laptop adapters and tiles
are organized around it.

But a thermal predictor that works on a laptop also works on a rack
server. The signals are different (RAPL/IPMI/Redfish instead of
asusctl/EPP), the actuators are different (the BMC owns the fans), and
the questions a user asks are different ("when will this rack
degrade?" vs "why is my laptop loud?"). The KNN that learns "this load
pattern means heat" doesn't care which side of the line you're on.

The P3 design records the split and makes it explicit. The same daemon
runs in both modes. The dashboard, the install plan, and the community
pointers all branch on the detected target.

## Two targets, defined

### Desktop target

The host has a graphical compositor or a consumer-grade
`/sys/firmware/acpi/platform_profile`. The hands that matter are
`asusctl_fan_curve_bias`, `epp_shift`, `ryzenadj_cap`. The signals that
matter are fan RPM, EPP, focused-app workload context, knee-band
crossings. The default mode is `cool` — anticipatory cooling with
opt-in armed actuation after calibration.

The questions a desktop user asks:

- Why is the fan loud right now?
- When will it ramp next?
- Can I quiet it without thermal-throttling?
- Is something running in the background spiking the load?

### Server target

The host has `ipmi_si` loaded, `ipmitool` available, or a Redfish
endpoint configured, *or* is headless without a laptop profile cycle.
The hands are limited to `readonly_log` and `notify_send` — the BMC
owns chassis fans, coolstep is monitoring infrastructure, not
controlling it. The signals are RAPL energy, BMC fan RPMs, inlet /
exhaust temps, PMU counters, eBPF scheduler events. The default mode
is `monitor` — actuator stays disabled permanently.

The questions a server admin asks:

- Which fan is starting to fail before it actually fails?
- Is paste degrading? How fast?
- What's the ETA to thermal trip at current load?
- Is the inlet temperature drifting between summer and winter?

## How the target is detected

`caps.facet` is a derived property of `PlatformCaps`. The logic:

1. Strong server signals first: `ipmi_si` kernel module loaded, or
   `ipmitool` on PATH, or `COOLSTEP_REDFISH_URL` in environment.
2. Virtual host without consumer power management → server.
3. Headless physical host without a laptop profile cycle → server.
4. Has a compositor or has `platform_profile` → desktop.
5. Otherwise → `ambiguous`, packaged as desktop by default.

EPP availability is deliberately *not* a desktop signal — modern Xeon
and EPYC CPUs expose EPP too. The earlier heuristic that treated EPP
as "this is a laptop" produced false positives on every modern server
and had to be removed.

The CLI flag `--facet=desktop|server|ambiguous` overrides the
auto-detection when needed.

## The three-layer manifest

A second decision packaged into P3 is that hardware support and
distribution-specific knowledge should not live in Python source code.
It should live in a JSON manifest that the package ships, the community
extends, and the user overrides — all without touching upstream code.

```
L0   coolstep/compat/core.json              bundled, read-only
L1   /etc/coolstep/community.json           managed updates
L2   ~/.config/coolstep/custom.json         user overrides, survives updates
```

The manifest contains the hwmon driver name sets (so a new MSI Super-IO
chip is a JSON PR, not a code PR), the distro ID-to-clan mapping (so
adding "EndeavourOS → arch" is a one-line change), the GPU vendor ID
table, the DMI virtualization hints, and the community-pointer
catalog.

### Merge semantics

The loader reads L0, then L1 (if present), then L2 (if present), and
deep-merges in that order. Dicts merge recursively. Lists replace by
default. Lists of dicts that have an `id` field merge entry-by-entry:
matching `id` deep-merges, new entries append, and
`{"id": "X", "_remove": true}` strips an entry. The
`{"_disable": true}` directive at any leaf removes that key from the
result.

This means a user who wants to use coolstep but disagrees with one
community recommendation can:

```jsonc
// ~/.config/coolstep/custom.json
{
  "community_pointers": [
    { "id": "bpftrace", "_remove": true }
  ]
}
```

…and never see the bpftrace pointer again, while still receiving all
other updates as the package and the community manifest evolve.

### Failure modes

L0 missing or corrupt is a fatal error — the daemon refuses to start.
L1 or L2 corrupt is a warning — the daemon logs the parse error and
skips that layer. The user's custom config taking precedence over
package updates is the explicit goal; we'd rather skip a malformed
override than refuse to run.

## Community pointers

A community pointer is a structured "you're missing something useful"
hint with the install command for the user's distro:

```json
{
  "id": "nct6687",
  "trigger": "MSI/Gigabyte/ASRock superio chip detected but no kernel driver",
  "reason": "Without nct6687 driver, motherboard fan RPMs are invisible",
  "commands": {
    "arch":   "yay -S nct6687-driver-dkms-git",
    "debian": "apt install dkms && git clone ...",
    "fedora": "dnf install dkms kernel-devel && ..."
  },
  "url": "https://github.com/Fred78290/nct6687d"
}
```

The seven shipped pointers cover the most common missing pieces:
`nct6687`, `asusctl`, `ryzenadj`, `perf_paranoid` (lockdown),
`rapl_perm` (CVE-2020-8694), `bpftrace`, and `ipmitool` (for the
server target).

The activation logic — which pointer to surface on which host — lives
in Python because it needs facet detection and capability checks. The
content lives in the manifest because it changes without code.

## Phase status

P3 ships in v0.5.0 with:

- `PlatformCaps` with 60+ fields and `facet` derived property
- `detect_caps()` reading from the three-layer manifest
- `caps_to_install_plan()` generating target-aware install plans
- `coolstep compat` CLI with `--facet`, `--manifest-paths`,
  `--install-plan`, `--json` flags
- Seven community pointers in `core.json`
- Five new server-class collectors (`rapl_energy`, `intel_i915`,
  `arm_thermal`, `redfish`, `ipmi`)
- Three new workload/perf collectors (`perf_events`, `ebpf_sched`,
  `dbus_session`)

## Planned follow-ups

The follow-up work breaks into four small sub-phases, none of which
blocks the v0.5.0 release:

- **P3.1** — dashboard URL parameter `?facet=server` reorders the tiles
  to emphasize foresight panels (drift, degradation forecast, fan
  health) over actuator history. Pure presentation.
- **P3.2** — `coolstep manifest update` command that fetches the latest
  L1 from a community-maintained GitHub raw URL with checksum
  verification.
- **P3.3** — `coolstep doctor --facet=server` adds server-specific
  health checks (BMC reachable, IPMI auth working, perf paranoia ≤ 2,
  RAPL readable).
- **P3.4** — `coolstep export --format=prometheus` endpoint for hosts
  that want to scrape the daemon from external Grafana.

## See also

- [architecture.md](architecture.md) — where the compat layer fits in
- [calibration-gates.md](calibration-gates.md) — the gates apply
  identically to both targets, but server hosts often bypass gate 2
- [stack-decisions.md](stack-decisions.md) — ADR-009 explains the
  `make() → None` discovery pattern that the manifest layers on top of
