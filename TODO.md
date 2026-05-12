# TODO

> **At-a-glance:** repo on P2.5 (2026-05-12). See
> `docs/p2.5-rollup.md` for full file inventory and the followup TODO
> at `~/.claude/projects/-home-zzalli/memory/project_coolstep_p2_5_followup_todo.md`
> (single source of truth for «what's next»).

## Phase P2.5 followup (the live list)

🔥 **BLOCKING**
- [ ] Unblock ChromaDB rust-bindings SEGV (pin `chromadb<1.0` OR migrate to `hnsw_rs`/`usearch`). Currently masked by drop-in `60-chroma-disabled.conf` → KNN dormant.
- [ ] Apply ryzenadj sudoers snippet (see `ryzenadj_cap.py` docstring); secondary: disable secure boot OR build `ryzen_smu` DKMS.

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
- [ ] [Race-to-Sleep TODO](../.claude/projects/-home-zzalli/memory/project_coolstep_race_to_sleep_todo.md)
- [ ] [HNSW migration TODO](../.claude/projects/-home-zzalli/memory/project_coolstep_hnsw_todo.md)
- [ ] io_uring batched reads in linux_sysfs (only if 16ms becomes a problem)

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
- [ ] gitea remote setup (`coolstep` repo на gitea.strong-host.net) + push master

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
- [ ] Server target priority для P4: PVE local (192.168.88.230) или EX44?
