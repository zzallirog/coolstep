# CLAUDE.md — coolstep root

> Главный navigator репы. Если ты Claude и зашёл сюда — **сначала прочитай
> этот файл**, затем перейди в нужный subdir по карте ниже. Не читай весь
> код — он избыточен. Используй карту.

**Repo version:** 0.5.11 (P2.10 — docs sync atop P2.8 HNSW cutover + P2.9 hotfixes, 2026-05-13)
**Walk protocol:** см. секцию «Self-update protocol» внизу.

> **P2.8–P2.11 ship-status (2026-05-13):**
> - **P2.8 (v0.5.9):** HNSW backend live via `COOLSTEP_KNN_BACKEND=hnsw`
>   (ADR-022) — `coolstep/adapters/storage/hnsw.py` drops in for
>   `ChromaStore` on the hot read path (5800× refresh speedup); migration
>   walked through `scripts/cutover_to_hnsw.sh`; rollback via
>   `scripts/hnsw_rollback.sh --restore-data`. Chroma SEGV resolved by
>   bypassing chromadb on the hot path. `COOLSTEP_CHROMA_DISABLED=1`
>   stays as emergency fallback. New modules: `core/knn.py` (selector),
>   `core/embedder_refit.py` (drift-triggered refit + atomic swap),
>   `core/storage_common.py` (shared metadata coercion).
> - **P2.9 (v0.5.10):** audit hotfixes — atomic writes for
>   `ml-state.json` + `runtime-state.json` (tempfile + `os.replace`);
>   `Store` read methods now hold the writer lock; `/api/predictor-cockpit`
>   moved into `asyncio.to_thread`; `MetaPredictor.predict` forwards KNN
>   neighbours / `danger_neighbour_count` / `suggested_rpm`;
>   `embedder_refit.refit_and_swap` hard-gated against `ChromaStore`.
> - **P2.10 (v0.5.11):** post-release docs sync — README badges + Quickstart
>   point at `coolstep_init.sh`; CLAUDE.md / architecture.md / stack-summary.md
>   reflect HNSW as recommended backend; troubleshooting.md gets the
>   `chroma-hnswlib` collision warning + HNSW-as-preferred-fix path;
>   drift-detection.md documents `DriftGate` + refit guards;
>   calibration-gates.md documents trust modes + cost overrides; module
>   `CLAUDE.md` re-synced to module surfaces.

---

## TL;DR за 30 секунд

- **Что:** локальная realtime ML, мягкое предотвращение перегрева до жёсткого throttle.
- **Архитектура:** core (platform-neutral) + adapters (per-platform plugins) + dashboard.
- **Где запускается:** target — ASUS TUF A15 (Ryzen 9 7940HS + Radeon 780M + RTX 4060M).
- **Roadmap:** P0 (foundation) → P1 (ML predictor) → P2 (soft actuators) → **P2.5 (adaptive curve + incidents + workload profiles)** → P3+ (desktop/server/win/mac).
- **Стэк:** Python 3.12 (3.14 venv on target), FastAPI + SSE, Lit (P2.4 redesign — INSTRUMENT identity + 6 themes), sqlite, HNSW via `chroma-hnswlib` (default since v0.5.9 — `COOLSTEP_KNN_BACKEND=hnsw`; ChromaDB pinned `<1.0` as fallback), без сети.
- **Privacy:** всё локально, никаких отправок.
- **Operational modes:** `cool` (default — anticipatory cooling) / `quiet` (subtractive bias on calm windows, safety eject @ 80°C) / `off` (notify-only).

---

## Карта дочерних модулей

```
coolstep/
│
├── CLAUDE.md  ◀ ты здесь
│
├── coolstep/                       package
│   ├── CLAUDE.md                   → entry-points, делегирование
│   ├── core/CLAUDE.md              → schema/ring/store/fingerprint/predictor/decision/calibration
│   ├── adapters/
│   │   ├── CLAUDE.md               → общий контракт adapter'ов
│   │   ├── collectors/CLAUDE.md    → telemetry sources (linux_sysfs, nvidia_nvml, amdgpu, hyprctl)
│   │   └── actuators/CLAUDE.md     → action sinks (readonly_log P0; asusctl/ryzenadj P2+)
│   ├── dashboard/CLAUDE.md         → FastAPI + SSE + Lit (P0 polish)
│   └── inspect/CLAUDE.md           → CLI: adapters/tail/stats/export
│
├── docs/CLAUDE.md                  → концепт, физика, архитектура, ADR'ы, calibration gates
├── tests/CLAUDE.md                 → pytest ~860 tests (859 collected, P2.10 sync 2026-05-13)
├── systemd/CLAUDE.md               → user-units (collector + dashboard)
└── data/CLAUDE.md                  → runtime store (gitignored)
```

Каждый дочерний CLAUDE.md содержит:
- **Purpose** — на что отвечает модуль
- **Invariants** — что нельзя ломать
- **Public surface** — что экспортируется наружу
- **Visit when** — когда вернуться сюда обновить
- **FAQ** — быстрые ответы
- **Sibling pointers** — связи на соседей

---

## Fast paths по типу вопроса

### «Как добавить новую платформу?»
1. → `coolstep/adapters/collectors/CLAUDE.md` (раздел Template)
2. → `coolstep/adapters/actuators/CLAUDE.md` (для действий)
3. → `docs/platform-support-matrix.md` (записать новый ряд)
4. → bump child version + sync здесь

### «Где transistor-efficiency curve / sweet spot?»
1. → `docs/efficiency-curve.md` (концепт + формула proxy)
2. → `coolstep/core/efficiency.py` (реализация)
3. live → `GET /api/efficiency` или dashboard tile «Thermal efficiency»
4. CLI → `coolstep efficiency --since 7d`

### «Модель драйфит — почему?»
1. → `docs/drift-detection.md` (indicators)
2. live → `GET /api/drift` или tile «Model drift»
3. CLI → `coolstep drift`

### «Что мы реально собираем?»
1. → `docs/telemetry-schema.md` (per-collector contributions table)
2. → `docs/workload-fingerprint.md` (что считаем поверх)
3. live → `GET /api/adapters` если dashboard поднят

### «Почему этот стэк, а не другой?»
1. → `docs/stack-decisions.md` (12 ADR'ов)
2. → `docs/physics-rationale.md` (Arrhenius rationale)

### «Что нужно для P1 (ML predictor live)?»
1. → `docs/calibration-gates.md` (все 8 gates)
2. → `coolstep/core/CLAUDE.md` (где placeholder predictor)
3. → `TODO.md` (checklist)

### «Где FAQ по конкретному модулю?»
- Каждый CLAUDE.md в subdir содержит секцию FAQ
- Если ответа нет — добавь его и bump child version

### «Что-то падает в продакшене»
1. `journalctl --user -u coolstep-collector -f`
2. `coolstep adapters` — кто discover'нулся
3. `curl http://127.0.0.1:18889/api/health` — daemon живой?
4. `curl http://127.0.0.1:18889/api/adapters` — costs не вылезли?

---

## Принципы (стабильные)

- **KISS.** Никаких спекулятивных абстракций. Три похожих строки лучше преждевременной обёртки.
- **Three layers, independently disable-able.** Telemetry → Predictor → Actuator. Каждый слой отключается отдельно через config.
- **Sandbox-first для hardware.** Любое hw-action: read-only sniff → dry-run симуляция → apply под флагом.
- **Realtime, edge-first.** Inference локально, никаких сетевых зависимостей.
- **Не заменяем реактивный контур.** thermald, asusctl, game-mode.service остаются. coolstep — слой выше, гасит пики до их порогов.
- **Per-host privacy.** Telemetry, модели, fingerprints — на этой машине, без отправки наружу.
- **Wrap, don't write.** Hardware actuators обёртки над asusctl/ryzenadj/nvidia-smi/EPP, не direct write в hwmon. ADR-010.

## Обязательные внешние конвенции

- `~/CLAUDE.md` — bash/python guidelines, doc-architecture
- `claw-skill-read --name coding-guidelines` — стиль кода
- `claw-skill-read --name coolstep-navigate` — этот workflow в виде skill (см. ниже)

---

## Git & GitHub

### Public remote

| Remote  | URL                                | Type                              |
|---------|------------------------------------|-----------------------------------|
| `github`| github.com/zzallirog/coolstep.git  | orphan-cut public release branch  |

Maintainer's internal mirrors (full history) are deliberately not listed here.

**Important:** `github/master` is an orphan branch — not linear with any
internal mirror.  Do **not** `git push github master` from an internal
remote without first verifying no private content (paths, hostnames,
working files) is in the diff.  Use the orphan-cut pattern for new
public releases.

### GitHub MCP

MCP server `github` is configured via the maintainer's local Claude
client for this project directory.  Active only when a Claude session is
opened in this checkout.

**Что умеет:** читать/создавать issues, PR, releases, смотреть commits на github.com/zzallirog/coolstep.

```
# Через gh CLI (всегда работает)
gh issue list --repo zzallirog/coolstep
gh release list --repo zzallirog/coolstep

# Через MCP — прямо в разговоре с Claude
```

---

## Self-update protocol

Цель: главный CLAUDE.md следит за тем, что дочерние не отстали.

### Когда master CLAUDE.md обновляется

- При **bump'е дочернего** module version → обнови соответствующий ряд в карте.
- При **появлении нового subdir** с собственным CLAUDE.md → добавь в карту + Fast paths.
- При **изменении цикла lifecycle / архитектуры верхнего уровня** → обнови TL;DR.

### Walk-the-tree (диагностика)

Запусти когда подозреваешь, что дочерние отстали:

```bash
# 1. найти все CLAUDE.md
find . -name CLAUDE.md -not -path './.venv/*' | sort

# 2. для каждого — извлечь Last synced
for f in $(find . -name CLAUDE.md -not -path './.venv/*'); do
    echo "$f: $(grep 'Last synced with master' "$f" | head -1)"
done

# 3. сравнить версии (Module version) с этим master CLAUDE.md
# Несоответствие = candidate для re-sync.
```

В будущем (P3+): автоматизировать через
[future skill `coolstep-claude-md-audit`].

### Версионирование

- **Repo version** (этот файл) — bump при ratified changes в верхнем уровне.
- **Module version** (каждый дочерний) — bump при изменении public surface
  (контракт, добавленный route, новое поле, переименование).
- **Last synced with master** — обновляется *здесь* при изменении master,
  *там* при изменении самого ребёнка. Sync = два рукопожатия.

### Будущая теория (P7+)

Каждый CLAUDE.md = endpoint в распределённой системе документации.
Самостоятельная сессия Claude (через dashboard) сможет:
- проверить целостность walk-tree
- предложить PR'ы на sync'у дочерних
- merge'ить как PR-merger через тот же dashboard

Всё это построено над протоколом, который описан выше — т.е. реализуется без
менять текстовый формат CLAUDE.md.

---

## Discovery manifest (Zabbix-LLD style)

В дополнение к этому документу каждый collector экспортирует **signal
manifest** через `signals()` метод — список `SignalDescriptor` с именем,
единицей, типом и зависимостями. Это позволяет:

- Dashboard tile «Discovered signals» рендерить каталог сигналов автоматически
- Research-tools писать аналитику без хардкода имён сигналов
- LLD-style автодобавление: новый sensor в kernel → discovery его подтянет
  при следующем sample

См. → `coolstep/adapters/collectors/CLAUDE.md` секция «Discovery manifest»
после Sprint 13.

---

## Status & roadmap

- **P0 done + Lit + discovery** (2026-05-03): foundation, 4 collectors, daemon active, dashboard на :18889 с 5 Lit components, discovery manifest 26 signals, 105 tests, distributed CLAUDE.md (10 children + skill)
- **Calibration window** (running): 14 дней passive run, нужны throttle events + игровой эпизод. Сервисы `coolstep-collector.service` + `coolstep-dashboard.service` enabled+started под user-systemd.
- **P2.3 quiet mode** (2026-05-12): второй op-режим (`REDUCE_NOISE` verb, knee subtractive bias, 3-layer safety belt, dashboard mode-switcher)
- **P2.4 adaptive curve + incidents** (2026-05-12): `core/curve.py` policy pipeline; `core/incidents.py` multi-angle similarity; `<incidents-tile>`; INSTRUMENT visual identity + 6 themes; per-action mode endpoints; modular shell (`instrument.js` + `pills/`)
- **P2.5 perf + ML/control hardening** (2026-05-12 night): linux_sysfs 19→16 ms (path+fd+selective cache); Conservative Mode; Workload Profiles (`Profile.{CODE,RENDER,GAME,IDLE,OTHER}`); heat-soak metric; danger vectors + spike-trigger data layer; equilibrium-RPM from KNN stable-state; efficiency-table background analyser; event segmentation; EWMA filter + snapshot archive + cluster drift modules
- **P2.5b interference + hw matrix test pillar** (2026-05-12): `tests/compat/fixtures/` synthetic platform harness (12 hw snapshots — ASUS TUF target, Framework AMD, Steam Deck OLED, ThinkPad Intel, Dell PowerEdge IPMI, HP ProLiant Redfish, Supermicro EPYC, RPi 5 ARM, Oracle Cloud ARM, Mac Mini Intel, Alpine LXC, Ryzen 7950X desktop). `tests/compat/interference/` 17 scenarios validating conflict resolution against PPD/TLP/asusctl/thermald/game-mode/Feral gamemoded/BMC/another coolstep — 4 gaps closed this session with real gates (S03 PPD/EPP, S04 TLP/ryzenadj, S13 Feral gamemoded probe, S16 sudoers preflight). `docs/{hw,interference}-matrix.md` + 107 compat tests
- **P1 (deferred)**: HDBSCAN classifier + XGBoost predictor — currently KNN_v1 is the predictor
- **P2.6 (filed)**: Race-to-Sleep vs Sustained discriminator (gated on real cpu_power_pkg)
- **P3-P6**: Linux desktop / server / Windows / macOS

Полный roadmap → `TODO.md`, `docs/platform-support-matrix.md`.
**Followup TODOs** → `[[project-coolstep-p2_5-followup-todo]]` memory + `docs/p2.5-rollup.md`.

---

## New modules (P2.4 → P2.9) — quick map

```
coolstep/core/
├── curve.py              ◀ NEW P2.4 — 8 composable policies + compose_curve
├── incidents.py          ◀ NEW P2.4 — multi-angle similarity (5 angles)
├── workload_profile.py   ◀ NEW P2.5 — Profile enum + resolver
├── event_segmentation.py ◀ NEW P2.5 — focus/load_jump/plateau segments
├── efficiency_calibration.py ◀ NEW P2.5 — stable-run analyser
├── cluster_drift.py      ◀ NEW P2.5 — workload-cluster Δ°C + DriftGate streak
├── ewma_filter.py        ◀ NEW P2.5 — confidence-adaptive smoothing
├── snapshot_archive.py   ◀ NEW P2.5 — golden-state archive
├── knn.py                ◀ NEW P2.8 — backend selector (COOLSTEP_KNN_BACKEND=chroma|hnsw)
├── embedder_refit.py     ◀ NEW P2.8 — drift-triggered refit + atomic HNSW swap (hard-gated against ChromaStore)
├── storage_common.py     ◀ NEW P2.8 — shared metadata coercion for ChromaStore + HnswStore
├── predictor_meta.py     ◀ NEW P2.8 — L1+L2 composition (TrajectoryBaseline + ResidualBank)
├── residual_meta.py      ◀ NEW P2.8 — Bayesian shrinkage + TrustMode (PRIOR/SHRUNK/CONFIDENT)
└── residual_log.py       ◀ NEW P2.8 — bounded-memory tail() streaming (P2.9 fix)

coolstep/adapters/storage/
├── chroma.py             ◀ existing, now routed through knn.py
└── hnsw.py               ◀ NEW P2.8 — hnswlib-backed KNN store + atomic swap recovery

coolstep/dashboard/static/
├── instrument.js         ◀ P2.4 — entry point (replaces dashboard.js)
├── pills/                ◀ P2.4 — per-pill modules (8 total incl. profile)
└── components/incidents-tile.js + event-segments-tile.js + predictor-cockpit-tile.js
```

Каждый NEW модуль — pure functions / immutable dataclasses. Daemon
wiring сидит в `coolstep/daemon.py`. См. `docs/p2.5-rollup.md` для
полного file-by-file inventory.
