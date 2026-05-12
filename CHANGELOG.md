# Changelog

All notable changes to coolstep are documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [semver](https://semver.org/).

---

## [Unreleased]

## [0.5.3] — 2026-05-12

Predictor-of-predictor: every forecast now carries a learned
correction keyed to the workload regime it was made in.  A live pilot
on a freshly switched `workstation-latency-quiet` thermal profile
revealed the previous TrajectoryBaseline systematically overshot by
~20°C on transition moments (load drop after a spike) — the predictor
was reading the instantaneous slope and assuming it'd hold, while the
new profile's active cooling was already pulling temperature down.

### Added

- **`coolstep/core/residual_log.py`** — append-only JSONL residual
  ledger.  Every prediction is held in a daemon FIFO until its
  horizon elapses; at that moment the (predicted, actual, residual)
  tuple plus the features-at-prediction-time is written to disk.
  This is the substrate the meta-predictor learns from.  Mirrors the
  existing JSONL pattern (incidents, decisions, actuator-journal):
  10 MB rotation × 3 generations, ~3 days history, ~80 bytes/record
  on average.  See ADR-016.
- **`coolstep/core/residual_meta.py`** — meta-predictor math layer:
    - `bucket_of(features)` quantises into `(load_band, slope_sign,
      accel_sign, profile_band)` — a 4-int key that stratifies the
      bias the user observed.  `slope_sign` and `accel_sign` need
      the new `cpu_temp_accel_per_sec_sq` feature.
    - `RunningStat` — Welford's online algorithm for mean+variance,
      plus an EWMA "compound" overlay for recency-biased mean.
    - `log_residual(r) = sign(r)·log1p(|r|)` — stabilising transform
      so a single 20°C outlier doesn't dominate the EWMA forever.
    - `ResidualBank` — `dict[BucketKey, RunningStat]` with
      `observe`, `correct`, `decay_all`, `from_log` (rebuild from
      disk on daemon startup).
    - `Cusum` — two-sided CUSUM detector for residual-mean drift.
      Triggers a soft decay when the meta-predictor itself drifts.
    - `compose_confidence(*p)` — additive on the log-odds scale.
- **`coolstep/core/predictor_meta.py:MetaPredictor`** — composes a
  base predictor (TrajectoryBaseline currently, KnnPredictor when
  Chroma returns) with the residual bank.  Drop-in replacement for
  any `Predictor`: model_name becomes `f"{base.name}+meta"`, the
  Prediction.reason carries the correction + bucket + sample count
  diagnostics.  See ADR-017.
- **`coolstep/core/tuned_profile_watch.py`** — polls
  `/etc/tuned/active_profile` every 30 ticks.  On change, the daemon
  decays the residual bank by 0.5 (preserve half the prior learning)
  and stamps `profile_changed_at` in ml-state.json.  Cusum is the
  secondary safety net for hosts without tuned-adm.  See ADR-019.
- **`coolstep/core/fingerprint.py`** — new features:
    - `cpu_load_slope_per_sec` — load trajectory (≈ proximate cause
      of upcoming cooling)
    - `cpu_temp_accel_per_sec_sq` — second derivative, computed by
      splitting the window in halves and differencing slopes.
- **`<predictor-cockpit-tile>`** — new dedicated tile, phase-space
  view (X = T, Y = dT/dt).  Past 30 s trajectory tail, predicted
  arrow forward, σ-ellipse around the prediction from rolling
  residual std.  Residual trail strip below shows the last 12
  validations as inline tokens (green/amber/red by magnitude).
  Accuracy badge in the header is the rolling-15-min `1 -
  median(|residual|)/10`.  No 800 ms validation pulse, no countdown
  ticker — the user found those annoying.  See ADR-018.
- **`/api/predictor-cockpit`** — single-roundtrip aggregator for
  the cockpit tile: current state + 30 s actual trail + 12-entry
  residual trail + accuracy + profile.
- **CLI**: `coolstep predict-debug --bucket K,K,K,K` and
  `coolstep predict-replay --since 1h` for inspecting the bank.
- **`scripts/predictor-eval.sh`** — bash orchestrator over the new
  CLI subcommands; coloured tabular output of live accuracy plus a
  per-bucket replay over a chosen window.

### Changed

- **Predictor dot removed from `<efficiency-tile>`** — the cyan
  ring / trajectory line / countdown / validation pulse / last-error
  label were a category error on the work/°C histogram (per ADR-018
  rationale).  The histogram is now clean again: mean / p95 / sweet
  / knee / a single live gold dot.  Predictor visualisation lives
  exclusively in the cockpit tile.
- **Daemon predictor selection** (`daemon.py`) now wraps both KNN and
  TrajectoryBaseline in `MetaPredictor`.  On startup the bank is
  rebuilt from `data/residual-state.jsonl`.
- **ml-state.json** surfaces `active_tuned_profile`,
  `profile_changed_at`, `meta_buckets`, `residual_log_count` for the
  cockpit tile.
- **`AUR pkgver`** — bumped both PKGBUILDs to 0.5.3.

### Fixed

- The ~20°C systematic overshoot the user observed on the post-tuned
  profile reduces to median-15m residual ≤ 0.5°C within ~30 seconds
  of restart (replay from residual log) and stays there as the bank
  collects bucket-specific corrections live.

## [0.5.2] — 2026-05-12

Two Intel-platform crash-fixes caught by a post-0.5.1 headless-server
pilot.  Both bugs hid behind "the file parses fine on my AMD laptop"
— the kind of break that only surfaces when someone else runs your
code on real hardware you don't own.

### Fixed

- **G-7 extended** — `<sparkline-tile>` now mirrors `<live-telemetry-tile>`
  italic-N/A treatment for structurally-absent platform readings (Intel
  iGPU power, RAPL CVE-locked, no-fan-tool RPM).  The pill says "data
  not available on this platform" instead of falling silently to `—`,
  which had read as "daemon broken" during pilot.
- **G-9 cpu_temp Intel fallback** — `Store.write_frame()` and
  `efficiency_calibration._tctl()` were hard-coding AMD-only sensor names
  (`tctl` / `tdie`).  On Intel hosts the `coretemp` driver exposes
  `Package id 0` instead, so `cpu_temp` was being written as NULL into
  sqlite — the `Live telemetry` tile read `CPU TCTL N/A` despite
  `temps_c['package']` clearly showing 49°C.  Fix chains: `tctl → tdie →
  package → max-of-cores` universal.  CI guard:
  `tests/test_cpu_temp_fallback.py` covers six cases (AMD primary /
  secondary, Intel package, max fallback, empty-dict None, and
  end-to-end via `Store.write_frame()`).
- **G-10 live-telemetry SyntaxError** — A backtick-wrapped em-dash
  (`` `—` ``) sitting inside a CSS comment **inside a Lit `css\`...\``
  template literal** silently closed the template string for the
  browser ESM parser.  Custom element never registered → empty
  `<live-telemetry-tile>` in DOM → main thermal panel just gone.  Worse:
  `node --check` happily approves the file; only browser ESM /
  `node --input-type=module -e "import(...)"` catches it.  Lesson
  filed in `docs/aur-publishing.md` § G-10.

## [0.5.1] — 2026-05-12

Interference & hardware compatibility test pillar **plus** AUR-readiness
gotchas batch.  Coolstep does not live in a vacuum; it shares thermal/power
knobs with PPD, TLP, asusctl, Feral gamemoded, BMC firmware, and (worst of
all) another coolstep instance.  This release makes those coordination
points visible and testable, closes four open conflicts with real actuator
gates, and lands seven AUR/packaging/UX fixes caught during a sandbox +
headless-server pilot deploy.

### Fixed — AUR packaging / dashboard / install-units (G-1..G-7)

Pre-AUR-cut sandbox test in two ephemeral Arch LXC containers plus a pilot
deploy on a headless Intel desktop tower (i5-13500 / Debian 13 / RAPL
locked / no fan tool) caught seven distinct cases.  All closed; CI guards added; full case study in
`docs/aur-publishing.md`.

- **G-1** `python-httpx` added to `checkdepends` in both PKGBUILDs —
  `fastapi.testclient` required it, `makepkg --check` was red on AUR users.
- **G-2** `coolstep/__init__.py:__version__` bumped to 0.5.1 (was drifted
  to 0.5.0 then 0.0.1 in the wheel metadata).  `coolstep/dashboard/server.py`
  FastAPI version now imports `__version__` instead of hardcoded `"0.0.1"`
  (third drift source closed).  Test `tests/test_version_sync.py` guards.
- **G-3** `coolstep-bench-gc.service` + `.timer` now `install -Dm644`'d by
  both PKGBUILDs.  Weekly `bench/runs/` GC now actually runs on AUR.
- **G-4** `coolstep install-units` detects headless session
  (`XDG_RUNTIME_DIR` unset / missing) and prints `loginctl enable-linger`
  instructions instead of bare `systemctl --user …` (which fails on rack
  servers with no graphical session).
- **G-5** `install-units` now `mkdir -p ~/coolstep/data/` so units don't
  loop on `status=226/NAMESPACE` (systemd `ReadWritePaths=` requires the
  path to exist at start time).
- **G-6** `[tool.setuptools.package-data]` extended to ship
  `coolstep/dashboard/static/*` (36 files: HTML/CSS/Lit/i18n).  Without
  this, `pip install` / `pacman -U` wheel was missing `index.html`, daemon
  API worked, `/` was 500.  Particularly toxic for enterprise SSH-tunnel
  access pattern (`ssh -L 18889:127.0.0.1:18889 host`).
- **G-7** `<live-telemetry-tile>` renders explanatory italic `N/A` with
  hover tooltip for fields the platform can't provide (Intel iGPU has no
  discrete GPU temp/power, RAPL CVE-locked, no fan tool) instead of silent
  `—`.  Critical for headless-via-SSH-tunnel admin UX — silent `—` read as
  «daemon broken», `N/A — reason` reads as «not applicable here».

### Added — CI guards for the AUR gotcha batch (`tests/`)

- `tests/test_version_sync.py` — `coolstep.__version__` ↔ `pyproject.toml`
  ↔ stable PKGBUILD `pkgver=` (G-2 guard).
- `tests/test_packaging.py` — every systemd unit shipped in PKGBUILDs
  (G-3), `python-httpx` listed in checkdepends (G-1), `static/` packaged
  in wheel + `pyproject.package-data` lists `coolstep.dashboard` (G-6),
  GET `/` returns 200 HTML smoke (G-6 functional, via `create_app()` factory).
- `tests/inspect/test_install_units.py` — headless + session branch text
  presence (G-4), data-dir creation + idempotency (G-5).

### Added — Hardware matrix harness (`tests/compat/fixtures/`)

- **`fake_platform()` ctx-manager** — materialises a synthetic `/sys + /proc
  + /etc` tree under `tmp_path` and patches `coolstep.compat.detect` so
  `detect_caps()` reads the fake substrate end-to-end. Snapshot DSL is a
  single Python dict per fixture; loader auto-handles default-arg patching
  for `detect_caps.__kwdefaults__` and `_epp_state.__defaults__`.
- **12 hardware snapshots** — ASUS TUF A15 (target), Ryzen 7950X+RTX 4090
  desktop, ThinkPad X1 Carbon (Intel), Dell PowerEdge R750 (IPMI),
  Raspberry Pi 5 (ARM), Framework 13 AMD, Steam Deck OLED, Supermicro
  H12 EPYC, HP ProLiant DL380 (Redfish), Mac Mini Intel + Fedora,
  Oracle Cloud Ampere ARM, Alpine LXC.
- **`test_hw_matrix.py`** — parametrised end-to-end check: each snapshot
  asserts `detect_caps()` fields + `caps_to_install_plan()` actuators +
  community pointers.

### Added — Interference test pillar (`tests/compat/interference/`)

- **17 conflict scenarios** validating coolstep's behaviour against other
  daemons that touch the same surfaces. Each scenario tagged
  `bugcase_status`: `covered` (gate verified), `gap` (proof-of-absence
  asserted, fix proposal filed), `design` (structural separation).
- **`docs/interference-matrix.md`** — catalogue with closed/open coverage
  and one-paragraph rationale per scenario. Companion `docs/hw-matrix.md`
  for the fixture-harness rationale.

### Added — Interference gates (real code, not just tests)

- **`epp_shift.make()`** cedes EPP ownership when
  `caps.power_profiles_daemon_active` or `caps.tlp_active`. Override via
  `COOLSTEP_EPP_SHIFT_FORCE=1`.
- **`ryzenadj_cap.make()`** cedes STAPM/PPT when `caps.tlp_active`.
  Override via `COOLSTEP_RYZENADJ_FORCE=1`.
- **`ryzenadj_cap._sudoers_preflight()`** — runs `sudo -n
  /usr/bin/ryzenadj --version` at startup; refuses to load the actuator
  if the NOPASSWD entry is missing, and logs the exact snippet to install.
- **`_is_game_mode_active()`** (in both `asusctl_fan_curve` and
  `game_mode_optimizer`) now probes Feral's system-level
  `gamemoded.service` alongside `--user game-mode.service`.

### Fixed

- **ChromaDB SEGV** unblocked: pyproject.toml pins `chromadb>=0.5,<1.0`
  (chromadb 1.x rust bindings crash on Python 3.14 during
  `Collection._get`). `tests/test_daemon.py` auto-skips when an unsafe
  combo is detected. Production drop-in `60-chroma-disabled.conf` stays
  as belt-and-suspenders.

### Open gaps (filed for v0.6.0)

Four interference scenarios remain open with concrete fix proposals in
`docs/interference-matrix.md` and `TODO.md` → `🛡️ INTERFERENCE GATES`:
S08 (two-instance fcntl lock), S10 (Hyprland socket verification),
S14 (`caps.nvidia_persistence_mode` field), S15 (asusctl curve drift
detection in `revert()`).

### Tests

- 684 passed, 25 skipped (chromadb autoskip on Py 3.14)
- 107 compat tests (88 pre-existing + 19 new in interference pillar)
- 0 regressions

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
