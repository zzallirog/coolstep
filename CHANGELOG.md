# Changelog

All notable changes to coolstep are documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [semver](https://semver.org/).

---

## [Unreleased]

## [0.5.0] — 2026-05-12

P3: two deployment targets (desktop responsiveness + server foresight) over one
ML brain, three-layer manifest topology, community pointers, target-aware install plan.

### Added — Compat layer (`coolstep/compat/`)

- **`PlatformCaps`** — immutable frozen dataclass with 60+ capability flags:
  distro/CPU/GPU/compositor/fan-tools/power-tools/systemd/kmod fields.
- **`detect_caps()`** — pure I/O detector reading `/proc`, `/sys`,
  `/etc/os-release`, env vars, `shutil.which`. Singleton + `reset_caps()` for tests.
- **`caps.facet`** derived property: `desktop` | `server` | `ambiguous`.
- **`caps_to_install_plan()`** — translates caps into actionable install
  recommendations per distro clan (Arch / Debian / Fedora / SUSE / NixOS).

### Added — Three-layer manifest

- **`coolstep/compat/core.json`** (L0, bundled, package-data) — single source
  of truth for hwmon driver sets, distro id-to-clan map, gpu vendor IDs,
  virtualization DMI hints, super-I/O prefixes, community pointers.
- **`load_manifest()`** in `coolstep/compat/manifest.py` — deep-merge
  L0 (core) ∪ L1 (`/etc/coolstep/community.json`) ∪ L2
  (`~/.config/coolstep/custom.json`).
- **Deep-merge semantics**: dicts recurse, lists replace, `{"_disable": true}`
  removes keys, `[{"id": "X", "_remove": true}]` strips id-keyed items.
- **`coolstep compat --manifest-paths`** — shows which layers loaded from where.
- **7 community pointers** in `core.json`: `nct6687`, `asusctl`, `ryzenadj`,
  `perf_paranoid`, `rapl_perm`, `bpftrace`, `ipmitool_server` with
  distro-specific install commands and upstream URLs.

### Added — Five new server-class collectors

- **`rapl_energy`** — Intel + AMD powercap `intel-rapl` interface, delta-based
  power computation, graceful CVE-2020-8694 permission handling.
- **`intel_i915`** — Intel iGPU via `i915`/`xe` drm; reads `gt/gt0/rps_act_freq_mhz`
  + throttle reason flags + hwmon temp/power.
- **`arm_thermal`** — ACPI `/sys/class/thermal/thermal_zone*` (primary source
  on ARM Graviton/Ampere/RPi/Asahi).
- **`redfish`** — BMC HTTPS via stdlib `urllib` (no extra deps); env-driven
  config (`COOLSTEP_REDFISH_URL/USER/PASS`); fans + inlet/exhaust + PSU.
- **`ipmi`** — `ipmitool sensor` subprocess parser; KCS local or remote LAN+.

### Added — Three "real" workload/performance collectors

- **`perf_events`** — long-running `perf stat -e ... -I 1000 -x , -a`
  subprocess + background reader thread; publishes cycles, instructions,
  cache-misses, CPI, cache_miss_pct.
- **`ebpf_sched`** — `bpftrace -f json` subprocess; per-CPU context-switch
  rate + fork/exec rates. Requires `CAP_PERFMON`.
- **`dbus_session`** — KDE Plasma + GNOME Shell via `gdbus` subprocess
  (no Python deps). Universal idle hint via `ScreenSaver.GetSessionIdleTime`.
  Skipped on Hyprland where `hyprctl` is canonical.

### Added — CLI

- **`coolstep compat`** — human-readable platform report.
- **`coolstep compat --json`** — JSON capability dump.
- **`coolstep compat --install-plan`** — package install recommendations.
- **`coolstep compat --facet=desktop|server`** — force target preset.
- **`coolstep compat --manifest-paths`** — show L0/L1/L2 chain.

### Added — Packaging

- **`LICENSE`** — MIT (matches `pyproject.toml`).
- **`packaging/aur/coolstep-git/PKGBUILD`** — bleeding-edge AUR flavor.
- **`packaging/aur/coolstep/PKGBUILD`** — stable release template.
- **`packaging/aur/README.md`** — `makepkg --check`, `.SRCINFO` generation,
  push-to-AUR instructions.
- **`CONTRIBUTING.md`** — what we accept / what needs discussion / coding
  conventions.
- **`pyproject.toml: [tool.setuptools.package-data]`** registers `core.json`.

### Changed

- **`linux_sysfs` HWMON sets** — `CPU/FAN/STORAGE/MEMORY/SKIP_HWMON_NAMES`
  now load from manifest at import time. Dictionary grew from ~5 drivers
  to 50+ entries (covers ASUS / MSI / Gigabyte / ASRock super-I/O chips:
  `nct6xxx`, `it87xx`, `f71xx`, `w83xx`).
- **`detect.py`** — `_detect_distro`, `_detect_gpu`, `_is_virtual`,
  `_detect_superio` read from `load_manifest()` instead of hardcoded dicts.
- **`install_plan._build_community_pointers`** — predicate-based selector
  reads pointer content from manifest; activation logic stays in Python.
- **All adapter `make()` functions** — opt-in `caps_if_set()` pre-check that
  short-circuits when caps are pre-detected, with graceful fallback so tests
  that don't initialize caps still work.
- **README.md** — full rewrite with "What IS / IS NOT", comparison tables,
  ASCII architecture, deployment-target comparison tables, three-layer manifest section, FAQ.

### Fixed

- **`.gitignore`** untracks rotating runtime jsonl
  (`decisions.jsonl*`, `actuator-journal.jsonl*`, `incidents.jsonl`,
  `runtime-state.json`, `asusctl_fan_curve_baseline.json`) to prevent
  auto-commit hook bloat.
- **`asusctl` version detection** — fallback chain
  (`asusctl --version` → `pacman -Q` → `dpkg -l` → `rpm -q`)
  works on v6+ which removed the `--version` flag.
- **`perf_events` discover** — 300 ms post-spawn poll catches kernel rejection
  on hosts that require `CAP_PERFMON` even at `paranoid=2`.

### Testing

- 628 tests pass (excluding `tests/test_daemon.py` — chromadb-py3.14 SEGV).
- 7 new test modules: `tests/compat/test_detect.py`, `test_install_plan.py`,
  `test_manifest.py`, `test_pointers_and_facet.py`, plus
  `tests/adapters/test_{rapl,intel_i915,arm_thermal,redfish,ipmi,
  perf_events,ebpf_sched,dbus_session}.py`.

---

## [0.4.0] — 2026-05-12

P2.0 through P2.5: full actuator stack, safety belts, stress harness, and dashboard expansion.
This release arms the system for live hardware writes on the target ASUS TUF A15 after the 14-day
calibration window. All actuators default to dry-run; live writes require `COOLSTEP_ACTUATOR_ENABLE=1`.

### Added

**Actuators (adapters/actuators/)**

- `asusctl_fan_curve_bias` — `RAMP_COOLING` verb; biases fan curve anchors in the 70–85°C knee band
  by `intensity_pct` (5–20%); discovers via `shutil.which("asusctl")` + version check (≥ 6).
- `ryzenadj_cap_boost` — `CAP_BOOST` verb; lowers STAPM/fast/slow limits via `ryzenadj` for AMD APU
  power cap during thermal pressure spikes.
- `epp_shift` — `SHIFT_POWER_ENVELOPE` verb; writes `balance_power` to all CPU EPP sysfs nodes
  (`/sys/.../cpufreq/energy_performance_preference`); restores per-CPU originals on revert.
- `notify_send` — `NOTIFY_USER` verb; `notify-send --app-name coolstep` desktop popup, gated by
  `COOLSTEP_NOTIFY_ENABLE` and `COOLSTEP_NOTIFY_COOLDOWN_S`.
- `readonly_log` promoted to persistent audit sink — writes every `apply()`/`revert()` event to
  `actuator-journal.jsonl` with JSONL size-based rotation (3 generations, 1 MB default).
- `game_mode_optimizer` (ADR-015) — P2.5 actuator that fires only when `game-mode.service` is
  active; cooperative model documented in `docs/game-mode-coordination.md`.

**Predictor**

- Trajectory-fallback signal in `core/predictor.py:_merge_with_trajectory` — physics-first overlay
  catches unfamiliar workloads KNN has never seen (rising slope ≥ 0.5°C/s above 78°C triggers
  `traj_prob=0.7`; already-hot at 85°C gives 0.65); merged via `max()` with KNN output.
- `intensity_pct` scales linearly with `throttle_prob` (range 5–20%) in
  `core/decision.py:_scale_intensity`.

**Decision engine**

- `Thresholds` extended: `min_arm_labeled_count=5`, `min_arm_confidence=0.5`, `rearm_gap_s=15.0`
  (`core/decision.py`).
- Env bypass hatches: `COOLSTEP_FORCE_FIRE_RAMP=1` (test-only, bench/stress.sh) and
  `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1` for pre-calibration stress runs.

**Daemon**

- `_armed_actions` dict (`daemon.py`) — tracks per-actuator `(action, applied_ts)` pairs.
- `_sweep_expired_armed()` — TTL auto-revert at the top of every tick; reverts any action past
  `expires_at + TTL_GRACE_SEC` without waiting for a new prediction.
- Shutdown revert: `daemon.run() finally` → `_revert_all_armed("shutdown")` covers clean SIGTERM.
- Multi-actuator audit routing: two-pass dispatch (audit pass → `readonly_log`; hardware pass → first
  supporting actuator); `_NO_REVERT_ACTUATORS` set exempts audit sinks.
- Incremental backfill every `COOLSTEP_BACKFILL_INTERVAL_TICKS` ticks (default 600, ~10 min) to
  label stale `was_hot_in_30s=-1` vectors without blocking the tick loop.
- `runtime-state.json` now persists `armed_actions[]` and `baseline_anchors` so the cleanup script
  can restore the user fan curve on SIGKILL/OOM.

**Safety belts**

- `systemd/coolstep-cleanup.sh` (`ExecStopPost`) — reads `runtime-state.json`; if `armed_actions`
  non-empty, restores curve from `asusctl_fan_curve_baseline.json` or falls back to `asusctl
  --default`. Runs even on OOM kill (Python `finally` does not).
- `asusctl_fan_curve_baseline.json` — snapshot of user's actual curve taken on first `apply()`; re-
  snapshotted every 5 min to keep stale-baseline window bounded.
- `COOLSTEP_ACTUATOR_ENABLE` kill switch hard-defaults to dry-run: commands are constructed and
  logged but `subprocess.run` is never called unless explicitly set to `1`/`true`.
- `COOLSTEP_GAME_MODE_DEFER=1` (default) — `asusctl_fan_curve_bias.supports()` returns `False` when
  `game-mode.service` is active, preventing coolstep from stacking a bias on top of game-mode's
  own tuning.

**Stress harness (bench/)**

- `bench/stress.sh` — S1..S4 scenarios (synthetic CPU / mixed iowait / game-mode / heat-soak);
  writes `data/stress-state.json` marker; captures `ml-state.jsonl` + `telemetry.jsonl` + `meta.json`
  per run into `bench/runs/<ts>-<scenario>/`.
- `bench/analyze.py` — stdlib-only post-run report: peak Tctl, time ≥ 85°C, first predict ≥ 0.65,
  first fan ramp-up timestamp, delta, ASCII sparkline.
- `bench/baseline.sh` — paired dry-run vs armed comparison for controlled before/after measurement.
- `bench/gc.py` — prune old bench runs, rebuild `bench/runs/index.json` summary.
- `systemd/coolstep-bench-gc.timer` — weekly bench-gc run (Sunday 03:00, randomized +10 min).

**CLI**

- `coolstep export-profile` / `coolstep import-profile` — portable host config round-trip
  (`coolstep/inspect/`).

**Scripts**

- `scripts/quickstart.sh` — new-host onboarding: venv, deps, unit install, smoke stress test.
- `scripts/health-report.sh` — diagnostic snapshot: adapter costs, calibration gate status, journal
  tail, store size.

**Dashboard (dashboard/static/)**

- `/api/actuator-journal` — last 200 JSONL lines from `actuator-journal.jsonl`.
- `/api/stress-runs` — bench run summaries from `bench/runs/index.json`.
- `/api/crash-recovery` — surfaces hard-crash recovery events from `runtime-state.json`.
- `/api/predictor-breakdown` — exposes trajectory vs KNN contribution per-tick.
- `/api/stress-state` — active stress scenario from `data/stress-state.json` (stale after 10 min).
- `<actuator-history-tile>` — last 20 apply/revert events, kind-colored, TTL countdown, collapsible
  cmd detail.
- `<stress-runs-tile>` — bench run index with peak Tctl, scenario, armed flag.
- Actuator count pill in masthead: `actuators: N`.
- Crash-recovery pill in masthead: surfaces non-clean revert events.
- Stress pill in masthead: `stress: idle / S1 / S2 …` polled every 5 s.
- Sparkline bias overlay band — second Y-series `fan_max_pwm_bias_pct` over fan RPM line.
- Predictor-breakdown tile — trajectory vs KNN contribution visualization.
- i18n EN/RU/UK lang switcher (`lang-store.js` + `strings.js` + `<lang-switcher>` component).
- Hero narrative layout: 3 cards → 2 cards (anticipate + physics rationale).
- Crimson Pro wordmark font; coverage pill 100% clamp.
- Tile grid reordered: pulse → health → model → reference.
- `<throttle-events-tile>` redesigned: hero summary + 7d×24h density heatmap + collapsible raw
  table (was raw table only).

**Makefile**

- 10 ops targets: `install`, `enable`, `restart`, `status`, `stress-smoke`, `health`, `bench-gc`,
  `export-profile`, `import-profile`, `install-units`.

**Docs**

- `docs/coolstep-stack-summary.md` — full-stack reference for new dev / new host; data-flow ASCII
  diagram, collector table, actuator table, tuning levers reference, first-run recipe.

### Changed

- `asusctl_fan_curve.py:revert()` — restores user curve via `--data` baseline anchors instead of
  `asusctl --default`; factory wipe remains only when no baseline snapshot exists (regression
  protection for quietify-mid custom curves).
- `systemd/coolstep-collector.service ExecStopPost` — changed from unconditional `asusctl --default`
  to state-aware `coolstep-cleanup.sh` that reads `runtime-state.json` and applies baseline restore.
- Daemon routing: first-match → audit-then-first-hw two-pass (audit sinks run on every action,
  hardware sinks get first-match only).
- KNN auto-fire thresholds tightened: `min_arm_labeled_count=5`, `min_arm_confidence=0.5`,
  `rearm_gap_s=15.0` (was no per-actuator gating).
- Bias `intensity_pct` now scales with `throttle_prob` (5–20%) instead of fixed percentage.
- `<throttle-events-tile>`: raw event table replaced by hero + density grid + collapsible details.
- Hero narrative: 3 cards → 2 cards (anticipate + physics rationale).
- `Thresholds` in `core/decision.py` extended with actuator-gating fields (non-breaking — new fields
  have defaults matching previous implicit behaviour).

### Fixed

- `hyprctl` collector: self-recover on late env propagation (`hyprctl.py:self-recover`); runtime-
  probe socket pattern for long-running processes avoids ENOENT on socket rotation.
- `nvidia_nvml`: removed loop-variable lambda closure (`B023×5`) — all lambdas were capturing the
  same final loop value.
- `linux_sysfs`: `iowait_pct` now emitted as a separate signal rather than folded into `load_pct`,
  preventing false-positive throttle_prob spikes on I/O-bound workloads.
- `tests/_FakeStore`: `query()` now honours `labeled_only` filter — unit tests were silently
  ignoring unlabelled entries and masking a KNN coverage bug.
- Journal-rotation tests: dropped `importlib.reload` in favour of `monkeypatch.setattr` to clear
  module-level state between test runs, fixing a state-leak that caused sporadic failures.
- `bench/stress.sh`: JSON bool literal fix — `armed: true/false` now passed through `ARMED_PY` env
  var for Python heredoc interpolation (was producing Python syntax error `armed: true`).
- Dashboard coverage pill: clamped to 100% to prevent > 100% display during rapid labelling bursts.

### Security

- `COOLSTEP_ACTUATOR_ENABLE` defaults to dry-run on all hosts — hardware actuators never execute
  `subprocess.run` unless the operator explicitly sets the env var.
- Baseline persistence (`asusctl_fan_curve_baseline.json`) prevents a hard crash from wiping the
  user's custom fan curve and forcing `--default`, which would expose the machine to unintended
  stock firmware thermal behaviour.
- Game-mode defer pattern (`COOLSTEP_GAME_MODE_DEFER=1`, default on) keeps coolstep out of Steam-
  active thermal contracts by default; operators must explicitly opt in to cooperative biasing.

---

## [0.3.0] — 2026-05-03

P1: KNN predictor live, efficiency curve, drift detection, and scientific baseline. 147 tests.

### Added

- `KnnPredictor` in `core/predictor.py` — 26-dimensional embedding, ChromaDB HNSW index, top-k=20
  neighbours, `prob / agreement / coverage / confidence` vote formula.
- Calibration gates (5 initial) in `core/calibration.py:evaluate()`: `coverage_hours`,
  `throttle_events`, `peak_amplitude`, `class_diversity`, `hyprctl_consistency`.
- Throttle FSM in `core/store.py` with hysteresis (`COOLSTEP_THROTTLE_ENTER_C=90`,
  `COOLSTEP_THROTTLE_EXIT_C=85`); persists episode start/end to `throttle_events` table.
- `runtime-state.json` — per-tick FSM state persistence; backfill cursor; survives daemon restarts.
- `core/efficiency.py` — thermal efficiency curve (Arrhenius proxy); `GET /api/efficiency`.
- Drift detector (`core/drift.py`) with 7 indicators: feature distribution shift, label imbalance,
  neighbour coverage drop, workload label entropy, etc.
- `ml-state.json` — predictor snapshot every 30 ticks: throttle_prob, confidence, features,
  calibration_ready, labeled_count, uptime, chroma stats.
- Z-score normalization in embedder; ensures features are scale-invariant across different hosts.
- `<neighbours-tile>` — top-K KNN neighbour viewer with workload label + distance.
- `<drift-tile>` — live drift indicator panel with 7 sub-indicators.
- `<efficiency-tile>` — thermal efficiency sweet-spot curve.
- `<calibration-state-tile>` — gate-by-gate calibration readiness progress.
- `<training-tile>` — labeled count, KNN warm-up status, model_name badge.
- Masthead pills: calibration-ready badge, ml-state age, daemon health, signals count.
- Operational threshold corrected to `COOLSTEP_HOT_THRESHOLD_C=82°C` (was 90°C — too conservative).

---

## [0.2.0] — 2026-05-03

P0 polish: discovery manifest, Lit components, and dashboard chrome. 110 tests.

### Added

- Discovery manifest (Zabbix-LLD style) — every collector exposes `signals()` → list of
  `SignalDescriptor` with name, unit, type, dependencies; aggregated at `/api/discoveries`.
  26 signals across 4 collectors at ship.
- Lit Web Components: initial 5 tiles (`live-telemetry-tile`, `sparkline-tile`, `adapters-health-tile`,
  `discovered-signals-tile`, `stack-rationale-tile`) replacing the minimal HTML dashboard.
- Dashboard token system (CSS custom properties) and atrium-style chrome: masthead, pill row, sidebar.
- `platform_state` probes in `linux_sysfs`: CPU governor, EPP mode, `platform_profile` ACPI value.
- Throttle event detector (threshold-crossing heuristic, pre-FSM) writing `throttle_events` rows.
- `core/store.py` SQLite WAL mode, 14-day frame TTL, 90-day throttle events TTL, periodic rotation.
- `core/fingerprint.py` — 26-dim feature extractor: `cpu_temp_max`, `slope_per_sec`, `load_p95`,
  `gpu_temp_max`, per-GPU power/util, workload-class one-hot encodings.
- Systemd user unit monitoring: `coolstep-collector.service` + `coolstep-dashboard.service` active.

---

## [0.1.0] — 2026-05-03

P0 foundation: core schema, ring, store, 4 collectors, daemon, CLI, and dashboard skeleton.
All sprints (A–I) completed in a single day. 98 tests.

### Added

- `core/schema.py` — `TelemetryFrame`, `Action`, `SimResult`, `ActionResult` dataclasses;
  `merge_partial()` for async collector accumulation.
- `core/ring.py` — fixed-capacity in-memory ring buffer (600 frames, ~10 min at 1 Hz).
- `core/store.py` — SQLite with WAL; `frames`, `throttle_events`, `actions` tables.
- `AlwaysIdleBaseline` predictor — stub returning `throttle_prob=0.0`; placeholder until KNN warms.
- `ReadonlyActuator` — logs `Action` intents to in-memory journal without hardware writes.
- `core/decision.py:DecisionEngine` — `Thresholds(notify=0.4, soft=0.7, hard=0.9, conf=0.6)` →
  `list[Action]`.
- 4 collectors in `adapters/collectors/`: `linux_sysfs`, `nvidia_nvml`, `amdgpu`, `hyprctl`;
  async `sample()` with per-collector timeout; `make()` returns `None` on absent hardware.
- `daemon.py` — 1 Hz tick loop: collect → embed → predict → decide → route; asyncio gather;
  `COLLECTOR_TIMEOUTS` dict.
- CLI subcommands via `coolstep/inspect/`: `adapters` (discover + cost probe), `tail` (live frames),
  `stats` (store summary), `export` (CSV dump).
- `systemd/coolstep-collector.service` + `systemd/coolstep-dashboard.service` user units.
- Dashboard backend (FastAPI, `:18889`): `/api/health`, `/api/telemetry/latest`,
  `/api/telemetry/range`, `/api/adapters`, `/api/sse/telemetry` (SSE 1 Hz).
- `pyproject.toml` with `[dev]` extras (pytest, coverage, ruff, mypy).
- `docs/`: `concept.md`, `physics-rationale.md`, `architecture.md`, `stack-decisions.md` (12 ADRs),
  `calibration-gates.md`, `telemetry-schema.md`, `workload-fingerprint.md`.
- Distributed `CLAUDE.md` navigation tree: 10 subdir children + master orchestrator + skill.

---

## Links

[Unreleased]: https://github.com/zzallirog/coolstep/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.5.0
[0.4.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.4.0
[0.3.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.3.0
[0.2.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.2.0
[0.1.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.1.0
