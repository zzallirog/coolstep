# TODO

> **At-a-glance:** repo on P2.5 (2026-05-12). See
> `docs/p2.5-rollup.md` for full file inventory; followup items are listed
> below under "Phase P2.5 followup". The new **Interference test pillar**
> + **hw matrix harness** landed 2026-05-12 — see
> `docs/interference-matrix.md` + `docs/hw-matrix.md`.

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
