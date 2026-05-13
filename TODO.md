# TODO

> **At-a-glance:** repo on P2.5 (2026-05-12). See
> `docs/p2.5-rollup.md` for full file inventory; followup items are listed
> below under "Phase P2.5 followup". The new **Interference test pillar**
> + **hw matrix harness** landed 2026-05-12 — see
> `docs/interference-matrix.md` + `docs/hw-matrix.md`.

## Phase P2.9 — Conservative-margin instrumentation (planned 2026-05-13)

Operator framing (design session 2026-05-13, после observing chronic
negative-residual spike в live cockpit):

> «Блин, а знаешь, он угадывает... мне казалось +30с это много, но это
> нормальная его работа. Мне просто нравилось что тикало каждую
> секунду и предиктило +5с.»

Key insight: large negative residuals are not predictor failure — они
показывают, что **cooling system выигрывает у historical hot envelope**
по KNN-найденному workload pattern.  Predictor pessimistically
matches against past hot history (safe direction for soft-cooling);
hardware-current trajectory остывает быстрее, чем тогда; разница =
installed margin.

Architecture не трогаем (KNN bias is intentional safe direction).
Меняем UX так, чтобы margin был **читабельным**, и state machine
понимала свою же conservative bias.

### P2.9.1 — Spike state exit on persistent negative residual
- [ ] `core/spike_detector.py`: add exit gate «closes when residual
      stays signed-negative beyond -N°C for M ticks» (defaults M=10,
      N=5).  Close as `predictor_margin_exceeded`, не как настоящий
      interactive spike.
- [ ] Tests: no false closures on transient cooling dip mid-hot;
      closes on sustained negative-residual margin.
- *Why: live cockpit показывает spike kitty 2043s open 34min с
  workload=kitty просто потому что residual стабильно −20°C → exit
  <2°C abs недостижим.*

### P2.9.2 — Signed ±err pills (cooling-wins green)
- [ ] `dashboard/static/pills/`: ±err 15m / 30s switch from abs() to signed:
  - residual < −threshold → green «err −X°C (cooling)»
  - |residual| ≤ threshold → green «err ±X°C»
  - residual > +threshold → red «err +X°C (overshoot)»
- [ ] Red reserved для under-prediction (real warning).  Cooling-beats-
      forecast — поэма coolstep'а, рендерим зелёным.

### P2.9.3 — Multi-horizon toggle (+5s / +15s / +30s)
- [ ] Backend: `/api/predictor-cockpit` экспортирует
      `forecasts: {h5, h15, h30}` — three saturation extrapolations
      из той же base/meta math (no extra inference cost).
- [ ] Frontend (`predictor-cockpit-tile.js`): segmented control chip
      `[ +5s | +15s | +30s ]` в header.  Active mode label «weight in
      premises: short / balanced / full».  Hero block Δ + canvas
      dashed forecast respect active horizon.  Persisted в localStorage.
- [ ] Default mode = +5s (reactive tick feel).
- *Why: operator: «нравилось что тикало каждую секунду и предиктило +5с».
  Все три horizon'а уже в math, UI просто выбирает render.*

### P2.9.4 — Dual-curve canvas (historical + current)
- [ ] Cockpit canvas: render two forecast lines simultaneously —
  - **historical envelope** (current dashed orange): KNN-anchored
    saturation, «исторически так разогревалось»
  - **hardware-current** (new solid green): pure Newton τ=4s saturation
    on `slope_short`, «physics на текущем slope удержит h секунд»
- [ ] Разница между линиями = installed margin (visible cooling
      improvement vs historical baseline).
- [ ] Legend strip: добавить две новые swatches.
- [ ] Respects active horizon из P2.9.3.

## Phase P2.8 — Memory layers sync (planned 2026-05-13)

Operator framing (live session 2026-05-13, после chroma SEGV recovery):

> «Важно настроить синхронизацию между двумя типами памяти и важности
> каждого отдельного. Но основной — больший. Он умнее. Короткий всего
> лишь ищет нарративы, и пробует угадать. Он слишком много ошибается
> без постоянной памяти.»

Three memory layers exist, weakly coupled:

| Layer | Storage | TTL | Role |
|---|---|---|---|
| LONG  | store.db + chroma KNN     | unbounded | smart archive, day/night, workload signatures |
| MED   | residual-state.jsonl      | rolling N | bucketed meta-correction (4 axes coarse) |
| SHORT | in-memory Ring + Newton τ | ~60s      | cockpit slope extrapolation |

Goal: connect them with confidence-weighted fusion so the long-term
archive dominates when it has a similar moment, short-term fills the
gap when archive is sparse, and meta-bucket bridges the cold-start
window.

### P2.8.0 — Warm-start reindex (urgent)
- [ ] `scripts/reindex_chroma_from_store.py` — sqlite frames →
      reconstruct TelemetryFrame → embedder.embed → chroma.add →
      backfill labels из throttle_events. One-shot, idempotent
      (skip ts already в chroma).  Verify post-run:
      `chroma_count ≈ frames count` и `confidence > 0.3`.
      *Memory anchor: live session 2026-05-13 — 41 869 frames в
      sqlite остались dead weight для retrieval после chroma SEGV
      recovery.*

### P2.8.1 — Fingerprint axes «когда» (P1, 1-2 days)
- [ ] `core/fingerprint.py`:
  - `hour_of_day` / `weekday` — циркулярные sin/cos pairs
  - `seconds_since_workload_change` — Ring track active class
  - `recent_idle_ratio_300s` — % фреймов cpu_load_max<10% за 5 мин
  - `compile_marker` — active_class ∈ {kitty, code, zen} AND
    rapl_pkg_jump > 20W/5s
- [ ] tests + docs/workload-fingerprint.md + bump core/CLAUDE.md
- *Why: KNN сам научится «открыл zen в 14:00 на холодном CPU → 5мин
  ramp 88°C» без эвристик. Все сигналы уже в raw frames.*

### P2.8.2 — Confidence-weighted fusion (P2, 1 day)
- [ ] `MetaPredictor.predict()` → mix формула:
  ```
  ΔT = w_long·KNN + w_med·meta_bucket + w_short·newton
  w_long  = sigmoid(neighbours_dist · n_samples_in_bucket)
  w_med   = sigmoid(n_samples_in_bucket / 100) · (1 - w_long)
  w_short = sigmoid(slope_persistence_3s)
  Σ weights → normalize to 1
  ```
- [ ] Каждый слой возвращает (estimate, self_confidence)
- [ ] Логировать dominant layer в ml-state.json:reason
- [ ] tests/test_meta_predictor.py
- *Why: «sync + priority» из запроса юзера 2026-05-13.*

### P2.8.3 — Snapshot archive trigger (P3, 0.5 day)
- [ ] `core/snapshot_archive.py` уже написан в P2.5, daemon trigger не wired
- [ ] Trigger: throttle event closed OR `predictor_spike` max|res| > 15°C
- [ ] Write: peak_ts + 60s контекст → `data/snapshots/<ts>.json`
- [ ] Retention: rotate после 200 файлов ИЛИ 7 дней (whichever first)
- [ ] `GET /api/snapshots` — galler-list для dashboard tile (P3.5)
- *Why: «идеально вижу snapshot состояния» — галерея эталонных эпизодов.*

## Phase P2.7 — Spike-driven training archive (started 2026-05-12)

Operator framing (live speedtest observation):

> «На резких новых задачах ему 5 тиков хватает, выровнять с запасом
> +2° (перебирает, пойдёт), потом стабилизируется почти мгновенно.
> Это нужно чтобы калибровать основной архив — заполнять его новыми
> неудавшимися образцами. Архив не менее важен — он как после
> калибровки, как после прочищенных вентиляторов. Эти фрагменты
> начинают отрабатывать когда предиктор ошибается, это сигнал о
> спайке.»

The "predictor surprised" moments are exactly the training data we
*should* be storing — they're auto-labelled by the residual itself.
The archive then becomes the answer to "what did this workload feel
like last time?" — distinct from the 5-second saturation forecast,
which only knows "what's the slope right now".

✅ **Detector + journal landed (commit 4-piece bundle 2026-05-12)**
- [x] `core/spike_detector.py` — state machine, entry 5°C/2-tick, exit
      2°C/3-tick.  10 tests.
- [x] Daemon wiring: feeds every validated residual; closure emits
      `Incident(kind="predictor_spike")` with workload + entry features.
      Routes through the existing incidents journal + multi-angle
      similarity, no parallel substrate.
- [x] Live state spliced into `/api/predictor-cockpit` (one round-trip
      for the cockpit; no separate endpoint).
- [x] Cockpit: ⚡ spike chip beside ±err pills when active; hidden
      when idle.  Pulsing border so eye picks it up without polling.

📦 **Pending: chroma backfill (gated on ChromaDB SEGV resolution)**
- [ ] When `COOLSTEP_CHROMA_DISABLED=0` (chroma reachable), feed
      `read_incidents(kind="predictor_spike")` rows back into the
      embedder as labelled training samples.  Weight =
      `max_abs_residual × duration_s` (severity × persistence).
      Higher weight ⇒ later KNN queries pull this neighbour harder,
      so the next zen/steam/speedtest fingerprint pre-arms cooling
      before the operator even sees the spike.
- [ ] Decay schedule: spike samples ≥ 30 days old halve in weight
      (the chip's thermal envelope drifts; old spikes are about a
      cooler/dustier fan than the current one).  Pure post-process at
      query time, no rewrite of the journal.
- [ ] Backfill on resolution: when the chroma flag flips, walk
      `incidents.jsonl` once and emit all `predictor_spike` rows as
      weighted vectors.  Idempotent — ChromaDB upsert on the same
      incident ts.

🧪 **Followups for the detector itself**
- [ ] Threshold tuning from live data: after a week of operation,
      look at the spike duration distribution.  If most close in
      ≤ 5 ticks, the 3-tick exit debounce may be too aggressive; if
      they hang at 60s+, the entry threshold is under-tuned and we're
      catching plateau drift, not real spikes.
- [ ] Surface a "recent spikes" tile or extend incidents-tile with
      a `kind=predictor_spike` filter chip.  Right now the operator
      sees the chip light up but can't browse what fired before.

## Phase P2.6 — Cockpit + mode UX (filed 2026-05-12)

UI feedback from a live stress-test session (12 screenshots, 5×20s
bursts + 10s idle cycles).  The cockpit v2 redesign held up
(relational metrics + time-axis canvas), and three follow-ups landed
in this session: 30s residual chip, canvas legend, trend-priority
audit pending.  Items below are what's still missing.

🎛️ **Mode pill in masthead is wrong widget**
- [ ] `static/pills/coverage.js` paints `pill-mode` with calibration
  readiness ("calibrating | ready") — but the masthead pill named
  *mode* should show the **operational mode** (`COOL` / `QUIET` /
  `OFF`).  Split into two pills (or rename one).  Source of truth for
  ops mode: `GET /api/mode`; `mode-switcher` component already reads
  it, the masthead pill needs to mirror the same state.

🔇 **Quiet mode — verify mechanism, not just label**
- [x] **Verified quiet does NOT pin/cap CPU frequency** (2026-05-12).
  `rg cpufreq|cpupower|scaling_governor|max_freq|min_freq` over
  `coolstep/adapters/actuators/` returns 0 hits.  EPP shift cedes
  to PPD (S03 gate).  Quiet biases only `quiet_subtract` policy in
  `core/curve.py` (knee subtraction up to QUIET_MAX_SUBTRACT=15° at
  the fan curve, eject at 80°C) — governor untouched.
- [ ] Document the quiet-mode promise so the next person doesn't
  misread it: *reacts softer, quieter, earlier · looks further ahead
  · records-archive priority > responsiveness · movements weaker but
  not absent · safety eject @ 80°C is the only hard gate*.
- [ ] If lookahead extension isn't actually wired yet (predictor uses
  same horizon=5s in all modes), add `quiet_horizon_sec` knob.

🚨 **Emergency button** (verified missing 2026-05-12: `POST /api/mode/emergency` → 404)
- [ ] No emergency endpoint exists today — only `cool/quiet/off`.
  Add `POST /api/mode/emergency` semantics:
    - revert all actuator biases immediately (asusctl curve →
      baseline, EPP → balance_power, RPM lifts dropped)
    - cancel any pending REARM_GAP_SEC waits
    - log incident with kind="user_emergency"
    - return to `cool` after the chip drops below knee + 5°C
- [ ] Add a separate big red button in the mode-switcher row, not
  just a 4th option in the radiogroup — semantically it's a verb
  ("eject all biases now") not a mode.

♻️ **Cool-mode inefficient-distribution reset**
- [ ] Detect when cool is producing *negative* efficiency (fans
  ramped, temp didn't budge, energy spent for nothing).  Source:
  `core/efficiency.py` already computes work_per_degree; threshold +
  rolling window TBD.  Action: drop the active bias, log incident
  kind="cool_inefficient", let the controller restart cleanly.

📊 **Coverage gate keeps resetting (3rd occurrence)**
- [ ] Investigate why `coverage_hours` returns to ~2-3h periodically.
  Live state right now: `2.66h / 24h target`.  Possible causes
  already seen: (a) sqlite wipe during dev, (b) clock skew on resume,
  (c) collector restart resetting in-RAM counters.  Audit:
    - is `coverage_hours` computed off `frames` ts range, or a
      separate counter?  (file: `core/calibration.py`)
    - does a `coolstep-collector` restart reset it?
    - does PVE-backup `data/store.db` rsync truncate WAL on the
      master?
  Once known: pin the source of truth + add an absolute floor (don't
  go down on counter rebuild).

🐛 **Cockpit trend priority audit (small)**
- [ ] When T ≥ 78°C *and* slope < 0, current trend phrase reports
  "cooling −6° / 30s" — formally true (slope·30s), reads as "safe"
  while chip is on a plateau peak.  Branch order in `_trend()`:
  check `T ≥ knee` BEFORE `s < 0` so the urgent state wins.
  (See shot-02 in /tmp/cockpit-shots/ from 2026-05-12 stress run.)

## Phase P2.5 followup (the live list)

🔥 **BLOCKING**
- [x] **ChromaDB SEGV** — pinned `chromadb>=0.5,<1.0` in pyproject.toml +
  autoskip in `tests/test_daemon.py` when chroma>=1.0 on Python 3.14
  (2026-05-12). Drop-in `60-chroma-disabled.conf` stays as belt-and-suspenders
  for live daemon.
- [ ] Apply ryzenadj sudoers snippet on TUF A15 (see `ryzenadj_cap.py` docstring +
  S16 in interference-matrix.md — `_sudoers_preflight()` will now refuse to load
  the actuator until the entry exists). Secondary: disable secure boot OR
  build `ryzen_smu` DKMS.

🛡️ **INTERFERENCE GATES** (`docs/interference-matrix.md`)
- [x] S03 — `epp_shift.make()` cedes EPP when PPD active (override via `COOLSTEP_EPP_SHIFT_FORCE=1`)
- [x] S04 — `ryzenadj_cap.make()` cedes STAPM/PPT when TLP active (override via `COOLSTEP_RYZENADJ_FORCE=1`)
- [x] S13 — `_is_game_mode_active()` in both asusctl + game_mode_optimizer probes Feral `gamemoded.service` (system unit) alongside `--user game-mode.service`
- [x] S16 — `ryzenadj_cap._sudoers_preflight()` refuses to load actuator until NOPASSWD entry installed
- [ ] S08 — inter-process fcntl lock so two coolstep instances can't both arm `epp_shift` at once. Path: `/run/coolstep.epp.lock`, `LOCK_EX|LOCK_NB`. If acquire fails, `make()` returns None.
- [ ] S10 — verify Hyprland socket exists before trusting `HYPRLAND_INSTANCE_SIGNATURE`. Currently env-var alone determines compositor; stale sessions lead to silent `hyprctl` failures downstream.
- [ ] S14 — add `caps.nvidia_persistence_mode` field (probe via `pynvml.nvmlDeviceGetPersistenceMode`); collector emits warning so calibration baselines aren't biased pessimistic.
- [ ] S15 — `AsusctlFanCurve.revert()` should re-read current curve (`asusctl fan-curve -j`) and skip restore if it diverged from baseline (= user manual override during the 300s TTL).

🔌 **WIRING** (modules ready, daemon plumbing pending)
- [ ] EWMA filter into `AsusctlFanCurve.apply()` output (module `core/ewma_filter.py` shipped + 14 tests)
- [ ] `rpm_target_lift` policy in `compose_curve` using `Prediction.suggested_rpm`
- [ ] Spike-trigger bypass of `REARM_GAP_SEC` when `danger_neighbour_count >= 3`
- [ ] `EventSegmenter` persistence → `data/segments.jsonl` + `/api/event-segments` reading it

🆕 **NEW IDEAS** (filed, not built)
- [ ] Time-weighted KNN (Layer 1 real-time adaptation — `weight = exp(-age/τ)`, τ ≈ 300s)
- [ ] Anomaly detection (lonely point in latent space → incident kind=`anomaly`)
- [ ] Cluster-drift dashboard tile (module `core/cluster_drift.py` shipped, no UI yet)
- [ ] Snapshot archive capture trigger (manual? post-cleaning? calibration-pass?)
- [ ] Race-to-Sleep vs Sustained discriminator (gated on real cpu_power_pkg)
- [ ] HNSW migration (Rust/C++ option if Chroma stays unstable)
- [ ] io_uring batched reads in linux_sysfs (only if 16ms becomes a problem)

## Phase P2.5b — Compat / interference test pillar (DONE 2026-05-12)
- [x] `tests/compat/fixtures/_loader.py` — fake_platform ctx + snapshot DSL
- [x] 12 hardware snapshots (asus_tuf_a15_7940hs, desktop_ryzen_7950x_rtx4090,
  thinkpad_x1_carbon_i7_intel, dell_poweredge_r750_server, raspberry_pi_5_arm,
  framework_13_amd_7840u, steamdeck_oled, supermicro_epyc_h12_server,
  hp_proliant_dl380_gen10_redfish, mac_mini_intel_haswell_linux,
  oracle_cloud_arm_ampere, alpine_lxc_container)
- [x] `tests/compat/interference/` — 17 interference scenarios, 5 closed gates,
  4 closed gaps (S03/S04/S13/S16) with real actuator code, 4 open gaps (S08/S10/S14/S15),
  3 structural separations (S05/S07/S09/S17)
- [x] `docs/hw-matrix.md` + `docs/interference-matrix.md`

## Phase R — Research (DONE 2026-05-03)
- [x] Hardware survey: ASUS TUF A15 FA507XV, Ryzen 9 7940HS + Radeon 780M + RTX 4060M
- [x] Existing tools landscape: thermald/tlp нет, есть power-profiles-daemon, ryzenadj, asusctl, game-mode.service
- [x] Web research: Arrhenius leakage + literature on ML proactive cooling (mdpi 2025: −96% hotspot duration; IET 2023: −12.5°C)
- [x] Стэк решения: Python 3.12 + psutil + nvidia-ml-py + amdgpu sysfs + sqlite + FastAPI + Lit (12 ADRs в docs/stack-decisions.md)

## Phase P0 — Skeleton (DONE 2026-05-03)
- [x] core/schema.py + ring.py + store.py (22 tests)
- [x] adapters/collectors/{__init__,linux_sysfs,nvidia_nvml,amdgpu,hyprctl}.py (32 tests, 4/4 live)
- [x] core/{fingerprint,predictor,decision,calibration}.py (24 tests)
- [x] adapters/actuators/{__init__,readonly}.py (7 tests)
- [x] coolstep/daemon.py + systemd/coolstep-collector.service (3 e2e tests)
- [x] coolstep/inspect/cli.py — adapters/tail/stats/export (smoke verified)
- [x] coolstep/dashboard/server.py + 7 routes + SSE + minimal HTML/CSS/JS (10 tests)
- [x] systemd/coolstep-dashboard.service на :18889
- [x] docs/{concept,physics-rationale,architecture,stack-decisions,workload-fingerprint,calibration-gates,telemetry-schema,platform-support-matrix}.md
- [x] **98 tests green**, 9 commits в master, real-hardware smoke passed

## Phase P0 — Calibration window (in progress)
- [ ] 14 дней passive collector run на target machine
- [ ] ≥ 10 throttle events accumulated (real-world usage)
- [ ] ≥ 1 эпизод Tctl > 85°C (heavy game / long build)
- [ ] ≥ 1 полный игровой эпизод через game-mode.service active
- [ ] systemd-cgtop проверка: collector < 1% CPU avg, < 50MB RSS
- [ ] sqlite < 200MB после 14 дней
- [ ] All calibration gates pass в `/api/calibration`

## Phase P0 — Polish (next session)
- [ ] Lit Web Components — заменить minimal HTML/JS на полноценные `<live-telemetry-tile>`, `<calibration-state-tile>`, `<stack-rationale-tile>`, `<workload-scope-tile>`, `<adapters-health-tile>` (требует браузерной верификации с user'ом)
- [ ] Browser smoke: открыть http://localhost:18889/, проверить что все 6 tile'ов рендерятся, SSE стримит

## Phase P1 — ML predictor (после P0 calibration green)
- [ ] core/classifier.py: HDBSCAN над rolling fingerprint vectors → workload labels
- [ ] core/predictor.py: XGBoostPredictor (заменяет AlwaysIdleBaseline)
- [ ] Throttle event detector (fills throttle_events table в store)
- [ ] Model train pipeline: nightly cron → train на собранном sqlite → save model + metrics в data/models/
- [ ] Dashboard: «Predictions live» tile активируется когда calibration ready + model.recall ≥ 0.6
- [ ] linux_perf collector — perf events для richer fingerprint
- [ ] Per-process GPU usage (nvidia-smi pmon, amdgpu fdinfo)

## Phase P2 — Soft actuators
- [ ] adapters/actuators/asusctl_fan_curve.py — `ramp_cooling` через asusctl
- [ ] adapters/actuators/ryzenadj.py — `cap_boost` через ryzenadj
- [ ] adapters/actuators/epp_shift.py — `shift_power_envelope` через EPP
- [ ] adapters/actuators/notify_send.py — `notify_user` через libnotify
- [ ] sandbox: dry-run симулятор + auto-revert по expires_at
- [ ] coordination: не наступать на game-mode.service (probe + skip)

## Phase P3+ — multi-platform (см. docs/platform-support-matrix.md)
- [ ] P3 Linux desktop verification (без iGPU, без Hyprland)
- [ ] P4 Server (Redfish/IPMI/cgroup)
- [ ] P5 Windows (LHM/powercfg)
- [ ] P6 macOS (powermetrics/pmset)

## Open от user'а — pending decisions
- [ ] Имя проекта: оставить `coolstep` или переименовать (`thermal-foresight`/`loadwise`/`arrhenius`)?
- [ ] Granularity sample: 1Hz default ок, или sub-second (5-10Hz)?
- [ ] Game-mode integration в P0: только observe (текущее) или сразу coolstep подсказывает?
- [ ] Server target priority для P4: home lab или rack server first?
