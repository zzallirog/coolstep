# coolstep — Full Stack Summary

> Written 2026-05-12. Audience: new developer or new host setup.
> For design rationale see `docs/stack-decisions.md`; for physics background see `docs/physics-rationale.md`.

---

## TL;DR

coolstep is a local, privacy-first ML daemon that predicts CPU thermal pressure on a laptop and applies
soft hardware interventions (fan bias, power cap, EPP nudge) *before* the firmware's reactive throttle
fires. The main loop runs at 1 Hz: collect telemetry → embed into ChromaDB → KNN predict + trajectory
overlay → decision engine → route to actuators. As of v0.3.0 the system is in a 14-day calibration
window on the target ASUS TUF A15 (Ryzen 9 7940HS + Radeon 780M + RTX 4060M); actuators are wired
and exercised in dry-run mode pending `calibration_ready=True`.

---

## Data Flow

```
  ┌──────────────────────────────────────────────────────────────┐
  │  SENSORS (hardware / OS / WM)                                │
  │  hwmon  /proc/stat  NVML  amdgpu sysfs  hyprctl clients     │
  └───────────────┬──────────────────────────────────────────────┘
                  │  sample() per-collector (async, with timeout)
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  COLLECTORS  (adapters/collectors/)                          │
  │  linux_sysfs · nvidia_nvml · amdgpu · hyprctl               │
  └───────────────┬──────────────────────────────────────────────┘
                  │  dict[str, object]  →  merge_partial()
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  TelemetryFrame  (core/schema.py)                            │
  │  cpu, gpus, fans, workload, platform_state, storage/mem temp │
  └──────┬────────────────────────────────┬──────────────────────┘
         │  ring.push()                   │  store.write_frame()
         ▼                                ▼
  ┌──────────────┐                ┌─────────────────────┐
  │  Ring        │                │  SQLite store.db    │
  │  (600 frames │                │  frames / throttle_ │
  │   in-memory) │                │  events / actions   │
  └──────┬───────┘                └─────────────────────┘
         │  window(600) → fingerprint.extract()
         ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  FEATURES  (core/fingerprint.py)                             │
  │  cpu_temp_max, slope_per_sec, load_p95, gpu_temp_max, …      │
  └───────────────┬──────────────────────────────────────────────┘
                  │  embed() → ChromaDB  +  query top_k=20
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  PREDICTOR  (core/predictor.py)                              │
  │  KnnPredictor: KNN vote + trajectory overlay → Prediction    │
  └───────────────┬──────────────────────────────────────────────┘
                  │  throttle_prob, confidence, horizon_sec
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  DECISION ENGINE  (core/decision.py)                         │
  │  Thresholds → list[Action]  (RAMP_COOLING / CAP_BOOST /      │
  │  SHIFT_POWER_ENVELOPE / NOTIFY_USER)                         │
  └───────────────┬──────────────────────────────────────────────┘
                  │  _route_action(): audit pass → hardware pass
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  ACTUATORS  (adapters/actuators/)                            │
  │  readonly_log  asusctl_fan_curve_bias  ryzenadj_cap_boost    │
  │  epp_shift     notify_send                                    │
  └───────────────┬──────────────────────────────────────────────┘
                  │  apply() → hardware / notify / dry-run log
                  ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  HARDWARE                                                    │
  │  asusctl fan-curve  ryzenadj --fast-limit  EPP sysfs         │
  └──────────────────────────────────────────────────────────────┘
```

Side-writes at each tick: `ChromaDB` (vector + metadata), `runtime-state.json` (FSM + armed actions),
`actuator-journal.jsonl` (every apply/revert event).

---

## Collectors

Four collectors run in parallel each tick via `asyncio.gather`. Each returns a partial dict;
`merge_partial()` in `core/schema.py` accumulates into one `TelemetryFrame`.

| Name | Source | Key fields | Typical cost |
|------|--------|-----------|--------------|
| `linux_sysfs` | `/sys/class/hwmon/`, `/proc/stat`, cpufreq, ACPI | `cpu.temps_c` (tctl/tdie), `cpu.load_pct[]`, `cpu.freq_mhz[]`, `fans[]`, `storage_temps_c`, `memory_temps_c`, `platform_state` (governor/EPP/profile) | ~18 ms |
| `nvidia_nvml` | pynvml (NVML SDK) | `gpus[].temp_c`, `.power_w`, `.util_pct`, `.mem_used_mb`, `.freq_mhz` | ~0.05 ms |
| `amdgpu` | `/sys/class/drm/card*/device/` hwmon + pp_dpm_sclk | `gpus[].temp_c`, `.power_w`, `.freq_mhz`, `.voltage_v`, `.util_pct` | ~1 ms |
| `hyprctl` | `subprocess hyprctl clients -j` (5 s cache) | `workload.label`, `workload.visible_apps`, `workload.proc_sigs[]` | ~0.2 ms (cached) / ~95 ms (refresh) |

`make()` returns `None` on any collector if the hardware or binary is absent — it is silently skipped
at discovery. Per-collector timeouts: 0.3 s default; `hyprctl` overridden to 2.0 s
(`daemon.py:COLLECTOR_TIMEOUTS`).

---

## Predictor Stack

`coolstep/core/predictor.py`. Two signals combined:

### KNN (primary)

`KnnPredictor` queries ChromaDB with the current frame's embedding vector, fetches `top_k=20`
nearest neighbours, votes on `was_hot_in_30s` label:

```
prob         = labeled_hot / labeled
agreement    = max(labeled_hot, labeled_cool) / labeled
coverage     = labeled / top_k
confidence   = coverage * agreement
```

Confidence is 0.0 when: embedder not fitted yet, no neighbours found, or `< max(3, top_k//4)`
neighbours have labels.

### Trajectory overlay (fallback / safety net)

Physics-first signal that catches novel workloads KNN has never seen (e.g. a fresh benchmark).
Runs in parallel; result merged via `max()`:

| Condition | traj_prob | Example reason |
|-----------|-----------|----------------|
| `cpu_temp_max ≥ 78°C` AND `slope ≥ 1.0°C/s` | 0.9 | rising fast |
| `cpu_temp_max ≥ 78°C` AND `slope ≥ 0.5°C/s` | 0.7 | rising medium |
| `cpu_temp_max ≥ 85°C` (steady) | 0.65 | already past Arrhenius knee |

**Merge rule** (`_merge_with_trajectory`): `throttle_prob = max(knn_prob, traj_prob)`.
When trajectory dominates: `confidence = min(0.7, knn_conf + 0.2)` — bumped so
`RAMP_COOLING` can clear the `min_arm_confidence = 0.5` gate.

`AlwaysIdleBaseline` (P0 stub) replaces KnnPredictor when ChromaDB is unavailable.

---

## Decision Engine Thresholds

`coolstep/core/decision.py:Thresholds`. Defaults below; no env overrides on the thresholds
themselves (env hatches are for the gate bypasses).

| Field | Default | Meaning |
|-------|---------|---------|
| `notify_prob` | 0.4 | `throttle_prob ≥ 0.4` → emit `NOTIFY_USER` |
| `soft_action_prob` | 0.7 | `throttle_prob ≥ 0.7` → `RAMP_COOLING` + `CAP_BOOST` |
| `hard_action_prob` | 0.9 | `throttle_prob ≥ 0.9` → `SHIFT_POWER_ENVELOPE` |
| `min_confidence` | 0.6 | global confidence floor (hard verbs + CAP_BOOST) |
| `min_arm_confidence` | 0.5 | relaxed floor for `RAMP_COOLING` only |
| `min_arm_labeled_count` | 5 | KNN index must have ≥ 5 labeled neighbours before `RAMP_COOLING` fires |
| `rearm_gap_s` | 15.0 | mirror of `REARM_GAP_SEC`; informational in engine |
| `ramp_cooling_temp_margin_c` | 0.5 | when already cooling, forecast must exceed current Tctl by this margin before `RAMP_COOLING` fires |
| `ramp_cooling_cooling_slope_c_per_sec` | -0.05 | short Tctl slope at or below this means the chip is already cooling |

`RAMP_COOLING` is also suppressed when the short live slope is cooling and
`expected_temp_c <= current_temp_c + ramp_cooling_temp_margin_c`. This keeps
the fan pre-spin from stacking on a recovery edge that a previous actuator
already created; `CAP_BOOST` and `NOTIFY_USER` keep their existing gates.

Residual learning is passive-first. `residual-state.jsonl` keeps every
validated prediction, but records whose horizon overlapped a real control
write carry `intervened=true` and `intervention_verbs`. Those controlled
records stay visible in the cockpit as dashed tokens/rings, while
`ResidualBank.from_log()` and live bank updates skip them by default.

Env bypass hatches (single source of truth in `DecisionEngine`, daemon forwards data only):

| Env var | Effect |
|---------|--------|
| `COOLSTEP_FORCE_FIRE_RAMP=1` | TEST-ONLY. Bypasses labeled-count gate, confidence gate, AND calibration gate for `RAMP_COOLING`. Used by `bench/stress.sh`. |
| `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1` | Allows `RAMP_COOLING` while `calibration_ready=False`. Other verbs stay blocked. |

---

## Actuators

All actuators are discovered at daemon start via `adapters/actuators/discover()`. Discovery is
import-based: every module in the package is loaded; `make()` returns `None` if the actuator
cannot operate on this host.

| Name | Verb | Discovery condition | Hardware action | Revert semantics |
|------|------|---------------------|-----------------|-----------------|
| `readonly_log` | ALL (audit) | always present | none — appends to in-memory journal + `actuator-journal.jsonl` | no-op; in `_NO_REVERT_ACTUATORS` |
| `asusctl_fan_curve_bias` | `RAMP_COOLING` | `asusctl` on PATH + version ≥ 6 + `COOLSTEP_ACTUATOR_ENABLE` ≠ false/0 | `asusctl fan-curve --mod-profile <P> --fan cpu --data <biased-anchors>` | re-apply saved baseline anchors via `--data`; fallback to `--default` if no snapshot |
| `ryzenadj_cap_boost` | `CAP_BOOST` | `ryzenadj` on PATH + `ryzenadj -i` succeeds + `COOLSTEP_ACTUATOR_ENABLE` ≠ false/0 | `ryzenadj --stapm-limit / --fast-limit / --slow-limit` (lowers ceiling by intensity_pct W) | re-apply saved baseline limits |
| `epp_shift` | `SHIFT_POWER_ENVELOPE` | `/sys/.../cpu0/cpufreq/energy_performance_preference` writable + `COOLSTEP_ACTUATOR_ENABLE` ≠ false/0 | write `balance_power` to all CPU EPP files | restore per-CPU original value from baseline snapshot |
| `notify_send` | `NOTIFY_USER` | `notify-send` on PATH + `COOLSTEP_NOTIFY_ENABLE` ≠ false/0 | `notify-send --app-name coolstep --urgency <low\|normal>` | no-op; notifications don't revert |

⚠ Default: `COOLSTEP_ACTUATOR_ENABLE` is unset → **dry-run** on all three hardware actuators
(asusctl, ryzenadj, epp_shift). Commands are constructed and logged but `subprocess.run` is never
called. Live writes activate only when `COOLSTEP_ACTUATOR_ENABLE=1` (or `true`).

`asusctl_fan_curve_bias` also checks `game-mode.service` state (cached 5 s) and returns
`supports(RAMP_COOLING) = False` when `COOLSTEP_GAME_MODE_DEFER=1` (default) and game mode is active.

---

## Routing in Daemon

`daemon.py:_route_action()` implements a **two-pass** dispatch per action:

1. **Audit pass.** Finds `readonly_log` by name; calls `apply()` unconditionally.
   Not tracked in `_armed_actions` (in `_NO_REVERT_ACTUATORS`). Failures are
   swallowed — audit must never crash the daemon.

2. **Hardware pass.** Iterates the actuator list, skipping names in `_NO_REVERT_ACTUATORS`.
   First actuator where `supports(verb) == True` gets `_do_apply()`, which:
   - checks re-arm guard: skip if existing action expires `> now + REARM_GAP_SEC` ahead
   - calls `actuator.apply(action)`
   - on success: writes to `_armed_actions[actuator.name] = (action, now)` and persists `runtime-state.json`
   - returns immediately (only one hardware fire per action)

If no hardware actuator supports the verb, the action is audit-only. This is the correct
behaviour for hosts where the hardware adapter didn't discover.

`_sweep_expired_armed()` is called at the start of every tick (before new prediction) to
auto-revert any action whose `expires_at + TTL_GRACE_SEC` has elapsed.

---

## Storage

| Artifact | Path (relative to `COOLSTEP_HOME`) | What persists | Restart behaviour |
|----------|------------------------------------|---------------|-------------------|
| `store.db` | `store.db` | SQLite: `frames` (14-day TTL), `throttle_events` (90-day), `actions` | survives restart; rotated every 600 ticks (~10 min) |
| ChromaDB vectors | `chroma/` | HNSW index: per-frame embedding + metadata (`ts`, `cpu_temp_at`, `was_hot_in_30s`, `peak_temp_after`, `workload_label`) | persistent; `was_hot_in_30s=-1` (UNKNOWN) backfilled to COOL/HOT by daemon on tick |
| `runtime-state.json` | `runtime-state.json` | throttle FSM state, `backfill_cursor_ts`, `armed_actions[]` | written per-tick AND on FSM transitions; read at startup to restore FSM + revert any stale armed actions |
| `ml-state.json` | `ml-state.json` | predictor snapshot every 30 ticks: throttle_prob, confidence, features, calibration_ready, labeled_count, uptime, chroma stats | lost on restart; regenerated after first 30 ticks |
| `asusctl_fan_curve_baseline.json` | `asusctl_fan_curve_baseline.json` | baseline fan curve anchors snapshot taken at first `apply()`; re-snapshotted every 5 min | survives restart; used by `coolstep-cleanup.sh` for SIGKILL recovery |
| `actuator-journal.jsonl` | `actuator-journal.jsonl` | append-only log of every `apply()` and `revert()` event across all actuators | survives restart; rotated at `COOLSTEP_JOURNAL_MAX_BYTES` (3 generations: `.jsonl`, `.jsonl.1`, `.jsonl.2`) |

SQLite tables: `frames(ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, workload_label, raw_json)`,
`throttle_events(ts_start, ts_end, duration, peak_temp, cause_label, workload_at_start)`,
`actions(ts, verb, params, actuator, dry_run, result, before_temp, after_temp)`.

---

## Safety Belts

1. **Python `finally` in `daemon.run()`** — on SIGTERM/SIGINT: `_revert_all_armed("shutdown")` +
   `_persist_runtime_state()` + `store.close()`. Covers clean shutdown.

2. **`ExecStopPost=coolstep-cleanup.sh`** (systemd unit) — runs after daemon exits regardless of
   reason including SIGKILL/OOM (Python `finally` does not execute there). The script reads
   `runtime-state.json`; if `armed_actions` is non-empty, restores the fan curve from
   `asusctl_fan_curve_baseline.json` (or falls back to `asusctl --default`). No-op on clean shutdown.

3. **Baseline restore in cleanup** — `revert()` on `asusctl_fan_curve_bias` prefers re-applying the
   saved baseline anchors via `--data` rather than factory `--default`, preserving user-tuned curves
   (incident 2026-05-12: `--default` wiped the quietify-mid custom curve on every restart).

4. **`COOLSTEP_ACTUATOR_ENABLE` kill switch** — global dry-run by default. Setting to `false` or
   leaving unset keeps all hardware actuators in log-only mode regardless of calibration state.

5. **`COOLSTEP_GAME_MODE_DEFER=1`** (default) — `asusctl_fan_curve_bias.supports()` returns `False`
   when `game-mode.service` is active, preventing coolstep from stacking a bias on top of
   game-mode's own tuning.

6. **TTL auto-revert** — every `Action` has an `expires_at`. `_sweep_expired_armed()` runs at the
   top of each tick; any armed action past `expires_at + TTL_GRACE_SEC` is reverted automatically
   without waiting for a new prediction.

**Failure modes and recovery:**
- Daemon crashes with armed bias → cleanup.sh reverts within the `ExecStopPost` window.
- ChromaDB dir bloat → `_chroma_size_guard()` (every 600 ticks) logs error at ≥5 GB / warning at ≥500 MB.
- `was_hot_in_30s` vectors stuck at UNKNOWN → `_recover_orphan_labels()` on startup re-labels old
  unlabelled vectors as COOL (conservative safe default).
- Stale `runtime-state.json` armed entry whose actuator is no longer discovered → logged and dropped
  without attempting revert.

---

## Dashboard Surface

FastAPI server on `:18889` (`coolstep-dashboard.service`). Reads from `COOLSTEP_HOME`; never writes
to SQLite. Separate process from the collector daemon.

### Tiles (Lit Web Components in `dashboard/static/components/`)

| Tile | Data source | Refresh |
|------|-------------|---------|
| `live-telemetry-tile` | SSE stream → `/api/sse/telemetry` (1 Hz) | live |
| `calibration-state-tile` | `/api/calibration` (core/calibration.py) | 30 s |
| `adapters-health-tile` | `/api/adapters` (live discovery + cost probe) | 30 s |
| `actuator-history-tile` | `/api/actuator-journal` + TTL countdown | 10 s |
| `discovered-signals-tile` | `/api/discoveries` (collector `signals()` manifests) | on load |
| `stack-rationale-tile` | `/api/stack-rationale` (parsed `docs/stack-decisions.md`) | on load |
| `stress-runs-tile` | `/api/stress-runs` (`bench/runs/index.json`) | 60 s |

### Key API routes

| Route | Returns |
|-------|---------|
| `GET /api/health` | `{daemon_seen, store_size_bytes, ml_state_age_sec}` |
| `GET /api/telemetry/latest` | newest frame row + raw_json blob |
| `GET /api/telemetry/range?since=15m` | range query from SQLite |
| `GET /api/calibration` | `{ready: bool, gates: [{name, passed, current, target, unit}]}` |
| `GET /api/ml-state` | raw `ml-state.json` snapshot |
| `GET /api/adapters` | `{collectors: [{name, cost_us}], actuators: [{name}]}` |
| `GET /api/actuator-journal` | last 200 JSONL lines from `actuator-journal.jsonl` |
| `GET /api/discoveries` | aggregated `SignalDescriptor[]` from all collectors |
| `GET /api/neighbours` | KNN top-K from `ml-state.json` |
| `GET /api/stress-state` | active stress scenario (or `{}` if stale) |
| `GET /api/stress-runs` | bench run summaries from `bench/runs/index.json` |
| `GET /api/sse/telemetry` | SSE stream, 1 event/s, `{ts, cpu_temp, …}` |

**Masthead pills** in `dashboard.js`: health indicator, calibration-ready badge, ml-state age.

---

## Calibration Gates

`coolstep/core/calibration.py:evaluate()`. All gates must pass for `calibration_ready=True`.
Evaluated every 60 ticks (~1 min). Coverage counted as `frame_count × COVERAGE_PERIOD_SEC`
(cumulative uptime, ignores suspend/reboot gaps — incident 2026-05-10).

| Gate | Measures | Default target | Env override |
|------|----------|---------------|--------------|
| `coverage_hours` | Total daemon uptime in hours | 168 h (1 week) | `COOLSTEP_COVERAGE_HOURS` |
| `throttle_events` | Rows in `throttle_events` table | 10 events | `COOLSTEP_THROTTLE_EVENTS` |
| `peak_amplitude` | `MAX(cpu_temp)` in `frames` | 85.0°C | `COOLSTEP_PEAK_TEMP` |
| `class_diversity` | `COUNT(DISTINCT workload_label)` excluding `''` and `'unknown'` | 5 clusters | `COOLSTEP_WORKLOAD_CLUSTERS` |
| `hyprctl_consistency` | % frames with active cpu_temp but no workload label | < 5% | `COOLSTEP_HYPRCTL_MISS_PCT` |
| `cost_rss` | RSS KB (when passed by caller) | < 50 000 KB | `COOLSTEP_COST_RSS_KB` |
| `cost_cpu` | CPU % (when passed by caller) | < 1.0% | `COOLSTEP_COST_CPU_PCT` |
| `ring_warmup` | `len(ring) >= ring.capacity // 2` | 300 frames | n/a |

---

## Tuning Levers

Every `COOLSTEP_*` env var accepted by the codebase. Set in `~/.config/systemd/user/coolstep-*.service.d/override.conf`
or export in shell for ad-hoc runs.

| Env var | Default | Controls | When to change |
|---------|---------|----------|----------------|
| `COOLSTEP_HOME` | `~/coolstep/data` | Root dir for store.db, chroma/, ml-state.json, runtime-state.json, journal | non-default install path |
| `COOLSTEP_HOT_THRESHOLD_C` | `82.0` | Lookahead label: frames where peak temp in next 30 s ≥ this → `LABEL_HOT` | host with different TjMax or thermal design |
| `COOLSTEP_THROTTLE_ENTER_C` | `90.0` | Throttle FSM: enter "hot" episode above this | conservative: lower; aggressive: raise toward TjMax |
| `COOLSTEP_THROTTLE_EXIT_C` | `85.0` | Throttle FSM: exit "hot" episode below this (hysteresis) | must be ≤ ENTER |
| `COOLSTEP_THROTTLE_MIN_S` | `3.0` | Minimum episode duration to write a `throttle_events` row | filter micro-spikes; lower = more events recorded |
| `COOLSTEP_TTL_GRACE_SEC` | `5.0` | Extra seconds past `action.expires_at` before auto-revert | raise if actuator revert is slow |
| `COOLSTEP_REARM_GAP_SEC` | `15.0` | Skip fresh apply if existing action expires less than this far ahead | reduce thrashing; raise if bias stacks badly |
| `COOLSTEP_ACTUATOR_ENABLE` | `dry-run` | `1`/`true` → live hw writes; `0`/`false` → disable actuator entirely; unset → dry-run | set `1` only after calibration window + manual verification |
| `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION` | unset (falsy) | Allows `RAMP_COOLING` before `calibration_ready` | `1` for early testing on a new host |
| `COOLSTEP_FORCE_FIRE_RAMP` | unset | TEST-ONLY: bypass all gates on `RAMP_COOLING` | `bench/stress.sh` only — never in production |
| `COOLSTEP_ASUSCTL_PROFILE` | `Performance` | asusctl profile name to bias | if your daily profile differs (e.g. `Balanced`) |
| `COOLSTEP_ASUSCTL_FAN` | `cpu` | fan target for asusctl bias | `gpu` on some models |
| `COOLSTEP_HYPRCTL_CACHE_SEC` | `5.0` | Seconds between `hyprctl clients -j` refreshes | lower = more responsive workload detection; higher = fewer subprocess forks |
| `COOLSTEP_HYPRCTL_TIMEOUT_S` | `2.0` | Per-call subprocess timeout for hyprctl | raise on slow systems; must be ≤ daemon `COLLECTOR_TIMEOUTS["hyprctl"]` |
| `COOLSTEP_GAME_MODE_DEFER` | `1` | `1` = defer (don't bias during game-mode); `0` = cooperative (bias on top) | set `0` if game-mode doesn't manage fans and you want coolstep to help |
| `COOLSTEP_NOTIFY_ENABLE` | `true` | `false`/`0` → disable desktop notifications entirely | headless host or suppressing popup noise |
| `COOLSTEP_NOTIFY_COOLDOWN_S` | `30.0` | Minimum seconds between desktop notification popups per verb | reduce spam under sustained load |
| `COOLSTEP_BACKFILL_INTERVAL_TICKS` | `600` | Ticks between incremental Chroma label backfill runs | lower = faster label propagation; higher = less I/O |
| `COOLSTEP_JOURNAL_MAX_BYTES` | `1 000 000` | Max size of `actuator-journal.jsonl` before rotation | raise for longer forensic history |
| `COOLSTEP_COVERAGE_HOURS` | `168.0` | Calibration gate: required cumulative daemon uptime | lower for rapid testing (e.g. `2`) |
| `COOLSTEP_THROTTLE_EVENTS` | `10` | Calibration gate: required throttle event count | lower for rapid testing (e.g. `1`) |
| `COOLSTEP_PEAK_TEMP` | `85.0` | Calibration gate: max CPU temp seen must reach this | lower if host never exceeds 85°C |
| `COOLSTEP_WORKLOAD_CLUSTERS` | `5` | Calibration gate: distinct workload labels | lower if host has fewer app types |
| `COOLSTEP_HYPRCTL_MISS_PCT` | `5.0` | Calibration gate: max allowed % frames without workload context | raise if locked-screen time is high |
| `COOLSTEP_COVERAGE_PERIOD_SEC` | `1.0` | Frame-to-seconds multiplier for coverage calculation | must match `--period` daemon arg |
| `COOLSTEP_T_AMBIENT` | `30.0` | Ambient temperature baseline for efficiency curve (`core/efficiency.py`) | set to measured room temp for accurate sweet-spot calculation |
| `COOLSTEP_COST_RSS_KB` | `50 000` | Calibration gate: max RSS in KB | raise if P1 ML inference adds memory |
| `COOLSTEP_COST_CPU_PCT` | `1.0` | Calibration gate: max daemon CPU % | raise if host is weaker |

---

## First-Run on a New Host

```bash
# 1. Install runtime deps (Arch / AUR)
sudo pacman -S python python-pip python-pipx asusctl ryzenadj libnotify

# 2. Clone and install in editable mode
git clone <repo-url> ~/coolstep
cd ~/coolstep
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Verify tests pass
python -m pytest -q --tb=short

# 4. Install systemd user units
make install-units   # copies systemd/*.service to ~/.config/systemd/user/
systemctl --user daemon-reload

# 5. Enable and start
systemctl --user enable --now coolstep-collector.service
systemctl --user enable --now coolstep-dashboard.service

# 6. Verify health
curl http://127.0.0.1:18889/api/health
# expect: {"daemon_seen": true, "ml_state_seen": false, ...}
# ml_state_seen becomes true after ~30 ticks (30s)

# 7. Check adapters discovered
curl http://127.0.0.1:18889/api/adapters
# expect: collectors list with linux_sysfs; nvidia_nvml / amdgpu / hyprctl if hardware present

# 8. Watch logs
journalctl --user -u coolstep-collector -f

# 9. Run a smoke stress test (exercises the hot path in dry-run)
COOLSTEP_FORCE_FIRE_RAMP=1 bash ~/coolstep/bench/stress.sh
# ✓ should see RAMP_COOLING in actuator-journal.jsonl

# 10. After calibration window passes (check /api/calibration all gates green):
#     enable live hardware writes in a drop-in override:
mkdir -p ~/.config/systemd/user/coolstep-collector.service.d/
cat > ~/.config/systemd/user/coolstep-collector.service.d/actuators.conf <<'EOF'
[Service]
Environment=COOLSTEP_ACTUATOR_ENABLE=1
EOF
systemctl --user daemon-reload
systemctl --user restart coolstep-collector.service
```

**⚠ Notes for non-ASUS hosts:** `asusctl_fan_curve_bias` will not discover if
`asusctl` is absent or version < 6. `ryzenadj_cap_boost` requires ryzenadj with
suid bit or running as root. `epp_shift` requires writable
`/sys/.../cpu0/cpufreq/energy_performance_preference` (amd-pstate-epp or intel_pstate
must be active). The system degrades gracefully — any missing actuator is silently
skipped; `readonly_log` always runs.

---

## Open Questions / Next Phase

Pending items tracked in `TODO.md`:

- **P0 calibration window** — 14-day passive run. Gates to clear: `coverage_hours=168`,
  `throttle_events=10`, `peak_amplitude=85°C`. Active since ~2026-05-03.
- **P1** — HDBSCAN workload classifier + XGBoostPredictor replacing `AlwaysIdleBaseline`;
  nightly training pipeline; linux_perf collector for richer fingerprint.
- **P2** — live actuator enable after P0 calibration + P1 predictor validated (recall ≥ 0.6).
- **P3–P6** — multi-platform: Linux desktop (no Hyprland/iGPU), server (Redfish/IPMI), Windows, macOS.

For architectural decisions (why Python, why sqlite, why ChromaDB, wrap-not-write) see
`docs/stack-decisions.md` (12 ADRs). For open design questions (project rename, sample granularity,
server priority) see `TODO.md:Open questions`.

Existing docs that predate v0.3.0 and are partially superseded by this document:
- `docs/architecture.md` — P0-era, no actuator detail, no trajectory predictor
- `docs/concept.md` — P0-era design goals, still valid for motivation
- `docs/stack-decisions.md` — ADRs, still current and authoritative for rationale
