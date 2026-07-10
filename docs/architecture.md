# Architecture

> Three layers, each one independently disable-able. Collectors observe,
> the predictor decides, actuators act — and any of them can be turned
> off without breaking the others.

## The tick loop

Once a second, the daemon does the same thing. (That "second" is the
deployed truth — the shipped systemd unit pins `--period 1.0`; running
`coolstep-collector` by hand without `--period` uses the faster 10 Hz
code default. See ADR-011.)

It walks the collector registry and asks each adapter for whatever piece
of the truth it can see. `linux_sysfs` returns CPU frequencies, hwmon
temperatures and per-core load. `amdgpu` returns GPU sclk and voltage if
an AMD card is present. `nvidia_nvml` returns the same for an NVIDIA
card via NVML. `hyprctl` returns the focused window class on a Hyprland
desktop; on KDE or GNOME, `dbus_session` returns it via `gdbus` instead.
On a rack server, `redfish` reads inlet temperature and fan RPM from the
BMC, `ipmi` does the same via `ipmitool` if Redfish isn't configured,
and `rapl_energy` reads the powercap counters for actual CPU package
power. `perf_events` and `ebpf_sched` add hardware-counter and
scheduler-event signals when the kernel permits them.

The collectors return partial `TelemetryFrame` dicts. The core merges
them into a single frame for this tick. The frame is appended to a
ring buffer (the last ten minutes, in memory) and written to a SQLite
store (14-day rolling, on disk).

The fingerprint extractor converts the frame into a 29-dimensional
embedding — temperatures, frequency rates, load percentiles, workload
class one-hot, GPU power, recent slopes, plus three trajectory features
(`cpu_temp_slope_5s`, `cpu_temp_accel`, `cpu_load_slope_5s`) so two
thermally identical snapshots with opposite slopes occupy different
regions of the KNN space. The embedding is z-score normalized so
different hosts compare cleanly.

The KNN predictor queries the ChromaDB HNSW index over historical
embeddings, takes the top-20 neighbours, and computes
`throttle_prob = weighted_vote(was_hot_in_30s)`. The probability is
merged with a physics fallback signal (a steep rising slope at high
temperature can fire the prediction even when KNN has no good
neighbours).

If `throttle_prob` clears the calibration-gated threshold, the decision
engine emits an abstract `Action` — `RAMP_COOLING` with an intensity
percent. The action goes to the actuator router: `readonly_log` always
records the intent for audit, and the first hardware actuator that
supports the verb gets the chance to apply it. The verb-to-command
translation lives in the actuator (e.g. `asusctl_fan_curve_bias`
translates `RAMP_COOLING +5%` into a curve patch and an
`asusctl fan-curve --mod-profile Performance --data ...` invocation).

Every action carries an `expires_at`. The next tick checks for expired
actions and reverts them. The daemon's shutdown path reverts everything
armed before it exits. A separate `coolstep-cleanup.sh` runs as
systemd `ExecStopPost` to handle the SIGKILL / OOM case.

## Module map

```
coolstep/
├── core/                      # platform-neutral
│   ├── schema.py              # TelemetryFrame, Action, SimResult, ActionResult
│   ├── ring.py                # in-memory rolling window
│   ├── store.py               # SQLite WAL with rotation
│   ├── fingerprint.py         # 29-dim embedding extractor (incl. trajectory features)
│   ├── predictor.py           # KNN + trajectory fallback
│   ├── decision.py            # thresholds → Action list
│   ├── calibration.py         # 8 gates between dry-run and armed
│   ├── curve.py               # 6-policy fan-curve compose pipeline
│   ├── incidents.py           # multi-angle similarity over throttle history
│   ├── workload_profile.py    # CODE / GAME / RENDER / IDLE classifier
│   ├── event_segmentation.py  # focus / load-jump / plateau segments
│   ├── efficiency.py          # work-per-degree curve
│   ├── drift.py               # 7 staleness indicators
│   ├── ewma_filter.py         # confidence-adaptive smoothing
│   ├── snapshot_archive.py    # golden-state archive
│   └── cluster_drift.py       # workload-cluster ΔT over time
│
├── compat/                    # the platform-detection layer
│   ├── caps.py                # PlatformCaps frozen dataclass
│   ├── detect.py              # OS / CPU / GPU / WM / tool detection
│   ├── manifest.py            # three-layer config loader
│   ├── core.json              # bundled baseline (L0)
│   └── install_plan.py        # caps → distro install recommendations
│
├── adapters/
│   ├── collectors/            # observe (read-only)
│   │   ├── linux_sysfs        # hwmon + cpufreq + /proc/stat
│   │   ├── amdgpu             # /sys/class/drm + pp_dpm
│   │   ├── nvidia_nvml        # pynvml SDK
│   │   ├── intel_i915         # i915 / xe gt0 + hwmon
│   │   ├── rapl_energy        # /sys/class/powercap (Intel + AMD)
│   │   ├── arm_thermal        # /sys/class/thermal/thermal_zone*
│   │   ├── hyprctl            # Wayland (Hyprland)
│   │   ├── dbus_session       # KDE / GNOME via gdbus
│   │   ├── redfish            # BMC HTTPS via stdlib urllib
│   │   ├── ipmi               # ipmitool subprocess
│   │   ├── perf_events        # `perf stat -I 1000` reader thread
│   │   └── ebpf_sched         # `bpftrace -f json` reader thread
│   │
│   └── actuators/             # act (or just log)
│       ├── readonly_log       # always-on audit
│       ├── asusctl_fan_curve  # ASUS knee-band bias
│       ├── epp_shift          # EPP balance_power shift
│       ├── ryzenadj_cap       # AMD STAPM / fast-PPT
│       ├── notify_send        # desktop popup fallback
│       └── game_mode_optimizer # cooperate with game-mode.service
│
├── dashboard/                 # FastAPI + Lit
│   ├── server.py              # 35 REST routes (polling; SSE removed — ADR-008)
│   └── static/                # Lit components, no bundler
│
└── inspect/                   # CLI
    ├── cli.py                 # 12 subcommands (adapters, tail, stats, compat, ...)
    ├── doctor.py              # health checks
    └── telemetry_export.py    # CSV / JSONL dump
```

## Three independent layers — disable anything

The architecture's central invariant: **every layer can be disabled
without breaking the others.** Not in theory — in practice, with a
single env var or systemd drop-in. The full matrix:

| Layer | How to run it alone | How to disable it | What still works |
|---|---|---|---|
| **Collectors** | `coolstep tail -n 60` (no daemon) | Per-collector: skip in `discover()`. Per-host: don't enable `coolstep-collector.service` | `coolstep compat` — read-only platform report |
| **Predictor** | Reads SQLite store, writes `ml-state.json`, no actuator wiring | `COOLSTEP_CHROMA_DISABLED=1` falls back to `MetaPredictor(TrajectoryBaseline)` (`trajectory_baseline+meta` — physics trajectory + meta-corrections, no KNN recall) | Dashboard, collectors, store still functional |
| **Actuators** | `COOLSTEP_ACTUATOR_ENABLE=true` arms them | `COOLSTEP_ACTUATOR_ENABLE=false` (the default) — every hardware-writing actuator constructs its command and logs intent without `subprocess.run` | Predictor still predicts, dashboard still shows what would have happened |
| **Dashboard** | `coolstep-dashboard` runs alone with read-only access to store | `systemctl --user stop coolstep-dashboard` | Daemon collects + predicts + actuates without UI |
| **Compat / manifest** | `coolstep compat` runs the detector standalone | Default L0 manifest always loads; L1 / L2 layers are opt-in | Adapters fall back to their own `discover()` checks |

This is what makes the read-only mode a first-class citizen instead of
a half-feature. Many users run only the collectors + dashboard for
weeks before deciding the predictor (and later the actuator) is worth
arming. Some never arm anything and use coolstep purely as a thermal
observability tool. The architecture treats both stances as equal.

The disable mechanism is not a config switch you have to find — it's
the absence of an opt-in. `COOLSTEP_ACTUATOR_ENABLE=false` is the
default. `pip install coolstep` does not start any daemon. `coolstep
compat` is read-only by construction. Every increase in coolstep's
footprint on your system is a step you took deliberately.

## Adapter contract

Every collector is a Python module under `adapters/collectors/` with a
single mandatory function:

```python
def make() -> Collector | None: ...
```

`make()` returns an instance if the host supports this adapter, or
`None` if it doesn't. The registry imports every sibling module, calls
`make()` on each, and silently drops the ones that return `None`. This
is how a single binary supports a laptop, a NUC and a rack: each host
lights up the subset of adapters that have something to read.

A `Collector` exposes four methods:

- `discover() -> bool` — idempotent capability check
- `sample() -> dict` — partial `TelemetryFrame` for this tick
- `cost() -> Cost` — rolling latency in microseconds
- `signals() -> list[SignalDescriptor]` — Zabbix-LLD-style manifest of
  what this collector emits

The `signals()` manifest is what makes the dashboard's "discovered
signals" tile content-driven instead of hardcoded.

Actuators follow the same shape with verbs instead of samples:
`supports(verb) -> bool`, `dry_run(action) -> SimResult`,
`apply(action) -> ActionResult`, `revert() -> None`.

## The compat layer

`coolstep/compat/` is the platform-detection layer that decides which
adapters could plausibly work on this host before any of them run their
own checks. It produces a single immutable `PlatformCaps` snapshot
describing distro, CPU, GPU, WM, available fan and power tools, kernel
modules and the eBPF / perf paranoia state.

The detection itself reads from a three-layer JSON manifest. The first
layer (`core.json`) ships with the package. The second
(`/etc/coolstep/community.json`) is where community-supplied driver
additions land — new super-I/O chips, new distro IDs, new community
pointers. The third (`~/.config/coolstep/custom.json`) is the user's
own override, which survives package updates because the package never
touches it. The merge is deep, lists replace, dicts recurse, and a
special `{"_remove": true}` directive strips items from id-keyed lists.

See [p3-plan.md](p3-plan.md) for the design rationale.

## Supporting infrastructure — what runs alongside the daemon

The daemon and dashboard are not the whole footprint. Below them sits a
small constellation of stores, append-only logs and snapshot files that
hold history, recover from crashes and let the dashboard render
without re-querying the live daemon. Everything below lives under
`$COOLSTEP_HOME` (default `~/coolstep/data/`). Nothing escapes that
directory.

### SQLite — the primary store

`data/store.db` in WAL mode. Five tables:

| Table | Contents | TTL | Typical size |
|---|---|---|---|
| `frames` | merged `TelemetryFrame`s, one row per tick | 14 days rolling | ~1.5–2 GB at 1 Hz |
| `throttle_events` | FSM episode boundaries with hysteresis | 90 days | tiny (<1 MB) |
| `actions` | what the actuator router emitted and when | 90 days | tiny |
| `meta` | key/value store metadata | permanent | tiny |
| `daily_rollup` | one row per UTC day × workload label (+ `_all` pooled row): temp / power / fan p50 / p95 / max, throttle-event count | **permanent, no TTL** | KB/day |

WAL means concurrent reads from the dashboard don't block daemon
writes. Vacuum runs lazily — the file grows slightly above the working
set and trims itself when the daemon is idle (`VACUUM` reclaims to
~1.5 GB).

`daily_rollup` is the long-horizon thermal ledger: `store.rotate()`
aggregates every complete UTC day *before* evicting its frames, so the
per-day percentiles survive the 14-day window forever. That is what
makes seasonal phase shifts (winter vs summer ambient),
thermal-interface degradation and dust build-up measurable years later
— same workload bucket, rising `temp_p95` / `fan_p95` at flat
`power_p50` = the cooling path got worse. Served by
`GET /api/thermal-history?since=YYYY-MM-DD&workload=X`.

### ChromaDB — the KNN backend (optional)

`data/chroma/` is the HNSW vector index. Populated by the daemon from
labeled `frames`, queried by the predictor for the top-20 cosine
neighbours. When ChromaDB is unavailable (notably on Python 3.14 due to
a known rust-bindings segfault — the adapter auto-detects ≥ 3.14 before
the import; `COOLSTEP_CHROMA_FORCE=1` opts back in) the predictor falls
back to `MetaPredictor(TrajectoryBaseline)` — physics trajectory
forecasts, no KNN recall, dashboard still works.

A watchdog in the daemon caps the chroma directory at 500 MB warn / 5
GB error (history at the chroma-bloat incident postmortem (internal archive)); the
collector self-disables the chroma write path before the dir can
explode.

### JSONL append-only logs

Three rotating files for forensics and dashboard drill-down:

| File | Contents | Rotation |
|---|---|---|
| `decisions.jsonl` | per-tick `throttle_prob`, features, confidence | 1 MB → `.1` → `.2`, 3 generations |
| `actuator-journal.jsonl` | every apply / revert / dry-run event | 1 MB, 3 generations |
| `residual-state.jsonl` | validated prediction residuals; `intervened=true` marks actuator-overlapped horizons | 10 MB, 3 generations |
| `incidents.jsonl` | multi-angle similarity hits flagged by `core/incidents.py` | unbounded (small) |

JSONL beats SQLite for these because tailing them is trivial, parsing
is line-by-line, and a corrupt last line never breaks earlier history.
The dashboard reads the active file with a 200-line tail.

### Snapshot files for crash recovery

Three single-file snapshots that survive SIGKILL / OOM:

| File | What it remembers | Read by |
|---|---|---|
| `runtime-state.json` | armed-action list, baseline anchors, FSM cursor | `systemd/coolstep-cleanup.sh` |
| `ml-state.json` | predictor's last snapshot every 30 ticks | dashboard `/api/ml-state` |
| `asusctl_fan_curve_baseline.json` | the user's fan curve before any bias | cleanup script + revert path |

The cleanup script is the second-tier safety belt. The first tier is
Python's `try/finally` in `daemon.run()` which reverts every armed
actuator on clean shutdown. If the daemon dies hard, the script reads
the snapshots and finishes the revert from outside.

### Where it adds up

A typical desktop install at steady state (14-day window full):

```
data/
  store.db                       ~1.5–2 GB
  chroma/                        ~50–200 MB
  decisions.jsonl{,.1,.2}        ~3 MB
  actuator-journal.jsonl{,.1,.2} ~3 MB
  incidents.jsonl                <1 MB
  runtime-state.json             1 KB
  ml-state.json                  10 KB
  asusctl_fan_curve_baseline.json 1 KB
─────────────────────────────────────────
                                 ~1.6–2.2 GB total
```

(`daily_rollup` lives inside `store.db` and adds only KB per day —
permanent, but negligible next to the 14-day frames window.)

Server-target hosts run smaller (no compositor signals, BMC is rate-limited).

## Modularity — three references for the same pattern

If you want to understand what "modular" means in this codebase
concretely, here are three places it's the same pattern repeated:

### 1. The `signals()` manifest — Zabbix-LLD style

Every collector exposes a `signals()` method returning
`list[SignalDescriptor]` — a typed declaration of what it can emit:

```python
def signals(self) -> list[SignalDescriptor]:
    return [
        SignalDescriptor(
            name="cpu.power_w", unit="W", dtype="dict[str, float]",
            source="/sys/class/powercap/intel-rapl/*/energy_uj",
            cardinality="dict",
            description="RAPL package + subdomain power, delta-derived",
            requires=("powercap kernel feature", "CAP_SYS_ADMIN for energy_uj"),
        ),
    ]
```

The dashboard's "discovered signals" tile aggregates all collectors'
manifests at `/api/discoveries` and renders the result without
hardcoding any signal name. Research tools query the same endpoint.
Adding a new collector means new fields appear in the dashboard with
zero UI changes.

### 2. The `make() -> Collector | None` discovery pattern

The registry walks every module under `adapters/collectors/`, imports
it, calls `make()`, drops the ones that return `None`. A new
collector is a single new file; existing code doesn't change.

Same shape for actuators in `adapters/actuators/`.

### 3. The three-layer manifest (`coolstep/compat/core.json`)

Hardware support — which hwmon driver names matter, which distro IDs
map to which package manager, which community pointers to surface
when something's missing — is JSON, not Python. The package ships
`core.json`. Communities can ship overlays at
`/etc/coolstep/community.json`. The user's `~/.config/coolstep/custom.json`
deep-merges on top and survives upgrades.

Adding a new fan-controller chip is one line in `core.json`. Adding
support for a new distro is two lines. None of that touches Python.

## User decisions vs coolstep decisions — the boundary

When the actuator is armed, three layers make decisions in sequence.
The boundary between them is explicit:

| Decision | Owner | Mechanism | Reversible? |
|---|---|---|---|
| Whether to install coolstep at all | **User** | `pipx install` or `pip install --user` | yes, `pipx uninstall` |
| Whether the daemon runs | **User** | `systemctl --user enable --now coolstep-collector` | yes, `disable --now` |
| Which profile to use (Quiet / Balanced / Performance) | **User** | `asusctl --profile Quiet` or platform_profile sysfs | yes |
| The baseline fan curve | **User** (set once via asusctl) | `asusctl fan-curve --data ...` | yes |
| **Whether the daemon may invoke privileged tools** | **User** | `sudoers.d/coolstep-ryzenadj` (NOPASSWD on `/usr/bin/ryzenadj`), or `AmbientCapabilities=CAP_SYS_ADMIN` in systemd drop-in, or polkit rule for asusctl | yes, `sudo rm /etc/sudoers.d/coolstep-ryzenadj` or remove the drop-in |
| Whether actuators can write to hardware | **User** | `COOLSTEP_ACTUATOR_ENABLE=true` | yes, set to `false` |
| What `--facet` to detect on this host | **User override** (or auto-detect) | `coolstep compat --facet=server` | yes, drop the flag |
| What L1 / L2 manifest overrides to apply | **User** | `~/.config/coolstep/custom.json` | yes, edit or delete file |
| **Within user's curve, when to bias** | **coolstep** | `throttle_prob ≥ gated threshold` | yes, auto-reverts in 30 s |
| **How much to bias** (within intensity cap) | **coolstep** | `intensity_pct = clamp(5, prob × 20, 20)` | yes, capped at 20% |
| When the bias expires and reverts | **coolstep** | `expires_at` per action | yes, on next tick |
| When to disarm because drift detected | **coolstep** | seven indicators in `core/drift.py` | yes, re-arms when drift clears |

The horizontal split: above the line is **user policy** that coolstep
respects without question; below the line is **coolstep judgement**
constrained by the policy above. coolstep never crosses the line
upward — it never auto-switches profiles, never edits your baseline
curve, never enables itself.

Three corollaries from this split:

- **The privilege grant is itself a user decision, and the most
  important one.** coolstep can never write to the fan curve, an MSR
  or RAPL unless the user has *first* installed a sudoers drop-in
  (`/etc/sudoers.d/coolstep-ryzenadj`), a systemd ambient capability
  (`CAP_SYS_ADMIN` in a `.service.d/` override), or a polkit rule
  (asusctl ships its own). Each of those is a file the user creates,
  owns and can remove. `sudo rm /etc/sudoers.d/coolstep-ryzenadj`
  revokes the grant in one command and survives every coolstep
  upgrade because coolstep itself never writes to `/etc/sudoers.d/`
  or `/etc/systemd/`. The full inventory of which privilege each
  actuator needs and how to grant the *minimum* for each lives in
  [privileges.md](privileges.md).
- The user can always make coolstep stop deciding anything by
  flipping `COOLSTEP_ACTUATOR_ENABLE=false`. The predictor still
  predicts, the dashboard still shows what *would* have happened, but
  the curve is yours again — without revoking the underlying sudoers
  grant.
- Every coolstep decision is auditable. `actuator-journal.jsonl`
  records the verb, the parameters, the timestamp, and the result.
  The privilege-using invocations (sudo, ambient cap) also land in
  `/var/log/auth.log` and `journalctl --user -u coolstep-collector`
  respectively. You can grep all three to answer "did coolstep touch
  the curve while I was playing yesterday?" without trusting any tool
  but `grep` and `jq`.

## See also

- [concept.md](concept.md) — the problem this design solves
- [telemetry-schema.md](telemetry-schema.md) — what each collector emits
- [curve-ownership.md](curve-ownership.md) — who owns the fan curve at
  each layer, where coolstep's authority ends
- [privileges.md](privileges.md) — what each actuator needs from the OS
  to actually write
- [stack-decisions.md](stack-decisions.md) — 21 ADRs covering why this
  stack and not another
- [p3-plan.md](p3-plan.md) — the two-deployment-target design and the
  three-layer manifest topology

## The dashboard

A FastAPI server on `:18889` with polled live telemetry (SSE was
removed — ADR-008, deprecated 2026-05-14) and a static
directory of Lit web components (no bundler, no build step — `esm.sh`
serves `lit@3` directly). Thirty-five REST routes, most read-only views
on the store and the live `ml-state.json` snapshot, plus three POST
endpoints (`/api/mode/{cool,quiet,off}`) for mode switching from the
dashboard pill.

The dashboard is the read-only face of coolstep and is the default
recommended way to interact with it. Even users who plan to arm
actuators eventually spend weeks here first.
