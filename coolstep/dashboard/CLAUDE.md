# CLAUDE.md — `coolstep/dashboard/`

> FastAPI бэкенд + frontend artifacts. Default port :18889 (атриум-стиль).

**Module version:** 0.2.0
**Last synced with master:** 2026-05-23
**Connectors:**
- ↑ package → `../CLAUDE.md`
- → reads from → `core/store.py`, `core/calibration.py`, `adapters/collectors/`
- → reads docs → `docs/stack-decisions.md` (ADR parser)

## Layout

```
dashboard/
├── server.py                          # FastAPI factory + routes
└── static/
    ├── index.html                     # entry: подключает 5 components + legacy dashboard.js
    ├── styles.css                     # масthead + grid + legacy tile styles
    ├── dashboard.js                   # legacy: health-pill + training tile + predictions placeholder
    └── components/
        ├── _base.js                   # Lit imports + fmtNum + tileBaseStyles
        ├── live-telemetry-tile.js     # SSE consumer, 6 KV pairs + age
        ├── calibration-state-tile.js  # gates checklist, 30s refresh
        ├── stack-rationale-tile.js    # ADR list with <details> expand
        ├── adapters-health-tile.js    # collectors + costs table
        ├── actuator-history-tile.js   # actuator journal timeline + TTL countdown
        ├── discovered-signals-tile.js # 26-sig manifest with collector chip filter
        └── stress-runs-tile.js        # bench/gc.py run summaries; verdict pill + hero ratio
```

## Routes

| Path | Returns | Source |
|---|---|---|
| `/` | index.html | static/ |
| `/static/*` | css/js | static/ |
| `/api/health` | daemon liveness | store.db mtime, ml-state.json mtime |
| `/api/telemetry/latest` | newest frame + raw_json | sqlite |
| `/api/telemetry/range?since=15m` | range query | sqlite |
| `/api/calibration` | gates state | core.calibration.evaluate |
| `/api/ml-state` | last predictor snapshot | data/ml-state.json |
| `/api/adapters` | live discovery + costs | adapters/collectors discover() |
| `/api/stack-rationale` | parsed ADRs | docs/stack-decisions.md regex |
| `/api/discoveries` | Zabbix-LLD signal manifest | aggregated collectors[*].signals() |
| `/api/neighbours` | KNN top-K snapshot from ml-state.json | persisted by daemon |
| `/api/actuator-journal` | Flat timeline of recent actuator apply()/dry-run | actuators[*].journal() |
| `/api/stress-state` | Active stress-test scenario (`{}` if absent/stale) | `data/stress-state.json` |
| `/api/stress-runs` | Per-run summary tail from bench/gc.py (`{runs:[…], count:N}`) | `bench/runs/index.json` (repo root, not COOLSTEP_HOME) |
| `/api/predictor-breakdown` | Final throttle_prob + features + server-recomputed `trajectory_prob_estimate` (mirrors `predictor.py:_trajectory_signal` thresholds) | `data/ml-state.json` |
| `/api/reliability` | Daemon reliability snapshot: `uptime_sec`, `restart_count`, `last_crash` (`{ts,kind,age_sec}` or null), `mtbf_sec`. | `systemctl --user show coolstep-collector` (`ActiveEnterTimestampMonotonic` + `NRestarts`) + `data/last-crash-recovery.json` |
| `/api/predictor-cockpit` | Cockpit-tile feed: spike-active, dual err pills (15m/30s), bucket strip, paired metrics | `data/ml-state.json` + residual bank |
| `/api/hot` | Live "hot now" signals (top contributors to throttle_prob right now) | in-memory predictor state |
| `/api/incidents` + `/api/incidents/{ts}/similar` | Incident archive + multi-angle similarity neighbours | `data/incidents.jsonl` |
| `/api/event-segments` | Focus / load_jump / plateau segments (P2.5 event-segmentation) | in-memory segmenter |
| `/api/throttle-events` | Throttle FSM events timeline | `data/throttle-events.jsonl` |
| `/api/drift` | Seven drift indicators (model has gone stale) | `core.drift.evaluate` |
| `/api/efficiency` + `/api/efficiency-table` | `work_per_degree` curve + stable-run analyser table | `data/efficiency_table.jsonl` |
| `/api/mode` (GET) + `/api/mode/cool` / `/api/mode/quiet` / `/api/mode/off` (POST) | Active operational mode + transitions | runtime-state.json + journal |
| `/api/profile` | Workload profile state (CODE / RENDER / GAME / BROWSER / IDLE / OTHER) | in-memory resolver |
| `/api/crash-recovery` | Last crash recovery event detail | `data/last-crash-recovery.json` |
| `/api/self` | Dashboard self-introspection (version, uptime, route count) | constants |
| `/api/debug/heap` + `/api/debug/trim` | Memory introspection + manual gc trigger (debug only) | runtime gc/tracemalloc |

## Invariants

- **Read-only по отношению к store.** Dashboard никогда не пишет в sqlite.
- **No state in process.** Все данные либо из store, либо из ml-state.json,
  либо из live discovery. Перезапуск сервера не теряет ничего.
- **Polling, not push** (balance-plan IV, 2026-05-14). `/api/sse/telemetry`
  был удалён — long-lived starlette streams удерживали парсенные frames
  в памяти и тянули RSS на ~2.7 GB за 2 мин. Dashboard теперь поллит
  `/api/telemetry/latest` каждую секунду; ADR-008 (SSE > WS) формально
  superseded для этого route.
- **Static dir mount только если существует.** Сервер должен подниматься без
  static/ (для headless smoke tests).
- **TestClient через `create_app()` factory.** Не использовать singleton
  app — иначе тесты делят state.

## Visit when

- добавляешь route → tests/dashboard/test_routes.py + этот файл (Routes table)
- меняешь schema sqlite → проверь все select'ы здесь
- добавляешь Lit component → `components/CLAUDE.md` (P0-polish, ещё нет)
- меняешь порт → `server.py:run_dashboard --port` default + `systemd/coolstep-dashboard.service` ExecStart

## FAQ

**Q: Почему порт :18889, а не :18879?**
A: :18879 занят `src.search.http` (vseoptef-catalog). :18889 свободен между
:18888 и :18890. Изначально было :18879 — поменяли в Sprint H smoke.

**Q: Почему index.html — plain HTML, а не Lit shell?**
A: index.html — host-shell, в нём раскиданы custom elements (`<live-telemetry-tile>`,
`<calibration-state-tile>`, ...). Lit живёт в `static/components/`. Lit
attaches к ESM через `https://esm.sh/lit@3` без bundler'а.

**Q: Где browser-side smoke?**
A: tests/dashboard/test_components.py — contract smoke (что файлы валидны,
что customElements.define вызывается). Visual render — пользователь руками
открывает http://127.0.0.1:18889/. Headless chromium на этом target
зависает на VAAPI; полная headless verification — после установки
playwright (P1).

**Q: SSE drops подключение через минуту. Это баг?**
A: Нет, нормально. uvicorn workers могут recycle подключения, browser
EventSource auto-reconnect'ит через 5s (см. dashboard.js).

**Q: Где чарты/графики?**
A: P0 нет. Нужен либо Chart.js, либо лёгкий sparkline через Canvas. → P1
полировка вместе с Lit components.

**Q: ADR парсер хрупкий?**
A: Regex `^## ADR-(\d{3}):\s*(.+?)$`. Если меняешь формат заголовков в
docs/stack-decisions.md — регэкс едет вместе. Тесты в test_routes.py:
test_parse_adrs.

## Sibling pointers

- → `../core/CLAUDE.md` — данные, которые рендерим
- → `../adapters/collectors/CLAUDE.md` — что probe'ится /api/adapters
- → `../../docs/stack-decisions.md` — источник /api/stack-rationale
