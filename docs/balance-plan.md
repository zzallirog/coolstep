# Balance plan — observer-effect contraction

**Filed:** 2026-05-14 (incident AGE 8s + slow tick `dump_ml_state` 2.3s)
**Status:** **SHIPPED 2026-05-14** — all 5 steps done in one session.
**Tracking memory:** `[[project-coolstep-balance-plan]]`

## TL;DR for tomorrow's you

5 steps done:
1. ✓ `busy_ratio` EWMA + ring in `daemon.py` → `ml-state.json:self_monitor.*`
2. ✓ `/api/self` + `<self-monitor-tile>` shows 8 self-cost signals (psi/mem/swap/age/tick) with TRIGGER state
3. ✓ Hot-state mmap in `/run/user/$UID/coolstep/state.mmap` + `/api/hot` — sub-second freshness vs 12s on JSON path
4. ✓ SSE removed — 4 tiles poll `/api/telemetry/latest` directly (ADR-008 deprecated)
5. ✓ `coolstep-shadow.service` unit shipped (disabled by default — start manually for 24h A/B)

Heap leak chase produced one real fix:
- **`/api/efficiency`**: TTL 30s → 300s + `gc.collect()` after build.
  Root cause was 307k `json.loads(raw_json)` calls per miss
  (efficiency.py:87) generating ~3M temp Python objects; GC couldn't
  keep up under back-to-back misses. Surfaced via new `/api/debug/heap`
  (tracemalloc endpoint, off by default — turn on with
  `PYTHONTRACEMALLOC=10` in service env).

Mitigations layered along the way:
- `MemoryMax` 1.5G/3G + `MemoryHigh` + `MemorySwapMax` (drop-ins `10-memory.conf`)
- `MALLOC_ARENA_MAX=2` + `MALLOC_TRIM_THRESHOLD_=131072` (drop-ins `91-malloc-tune.conf`) — modest help, kept
- `RuntimeDirectory=coolstep` drop-ins for both services (else `ProtectSystem=strict` blocks tmpfs write)

## How to verify tomorrow

```
# Are services healthy?
systemctl --user status coolstep-{collector,dashboard}

# What does the self-monitor say right now?
curl -s http://127.0.0.1:18889/api/self | jq

# Is the hot path under 1s?
curl -s http://127.0.0.1:18889/api/hot | jq '.age_sec'

# UI: open http://127.0.0.1:18889/ — bottom-right tile is "Self-monitor".

# Re-enable tracemalloc for incident chase:
#   edit ~/.config/systemd/user/coolstep-dashboard.service.d/92-tracemalloc.conf
#   systemctl --user daemon-reload && systemctl --user restart coolstep-dashboard
#   curl 'http://127.0.0.1:18889/api/debug/heap?top=25' | jq

# Run the 24h A/B (step V — kept disabled by default):
systemctl --user start coolstep-shadow
# Compare residual-log.jsonl between ~/coolstep/data/ (armed) and
# ~/.local/state/coolstep-shadow/ (read-only). Stop with:
systemctl --user stop coolstep-shadow
```



## Motivation

coolstep живёт в «коробке»: с одной стороны — не должен влиять на железо
(локальный observer), с другой — влияет на собственные предикты через
свою же нагрузку (recursive observer effect). Когда коробка протекает
(OOM, swap thrash, slow ticks) — predictor видит шум от самого себя,
не от observed workload.

Этот документ — план контракции коробки до уровня, где self-cost
quantifiable и **видимый в UI как отдельный тайл**, не как silent
background влияние.

## User decisions captured (2026-05-14)

1. **D.2 hot-state mmap в tmpfs** — выбран вместо `dump-on-change` JSON.
   Path: `$XDG_RUNTIME_DIR/coolstep/state.mmap` (= `/run/user/1000/coolstep/`).
2. **E.2 SSE removed** — заменить на client `setInterval(1000)` + `GET /api/telemetry/latest`.
3. **Tile name:** `<self-monitor-tile>` "Self-monitor" (не «Pulse» — слишком нетехнично).
4. Порядок реализации — на усмотрение исполнителя.

## Steps (suggested order — independent unless noted)

### I. `busy_ratio` metric внутри `daemon.py` ★ base for II

- Файл: `coolstep/daemon.py` — там где складываются `stages` dict.
- Aggregate: `busy_ratio = total_tick_ms / (period_sec * 1000)`.
- EWMA: α=0.1 на ring buffer 60 ticks (~12s @ 5Hz).
- Output: новое поле в `data/ml-state.json`
  → `daemon.busy_ratio_ewma`, `daemon.busy_ratio_p95_60s`.
- **Тесты:** `tests/test_daemon_busy_ratio.py` — synthetic stage timings, EWMA proof.

### II. `<self-monitor-tile>` + новый endpoint `/api/self`

Aggregator всех self-signals в один JSON:

```json
{
  "busy_ratio": {"ewma": 0.12, "p95_60s": 0.18, "trigger": false},
  "psi": {
    "cpu": 0.04, "mem": 0.0, "io": 0.01,
    "source": "/sys/fs/cgroup/.../coolstep-collector.service/cpu.pressure"
  },
  "memory": {"rss_mb": 169, "limit_mb": 1536, "swap_mb": 0, "trigger": false},
  "tick": {"period_sec": 0.2, "slow_count_60s": 1, "trigger": false},
  "collectors": {"linux_sysfs_timeouts_1h": 0, "trigger": false},
  "chroma": {"knn_empty_results_1h": 0, "trigger": false},
  "ui": {"age_max_60s_sec": 0.4, "trigger": false},
  "restarts": {"nrestarts_total": 0, "last_crash": null, "trigger": false}
}
```

- **Файл:** `coolstep/dashboard/server.py` — новый route.
- **Frontend:** `coolstep/dashboard/static/components/self-monitor-tile.js`.
- **Visual contract:**
  - Ряд dot'ов · · · · — зелёный пока всё OK, красный если trigger.
  - Под dot'ами строка-журнал последних TRIGGER'ов (timestamp + сигнал).
  - Микро-плюшка справа: `Self-cost: cpu 12% / ram 169M / age 0.4s`.
- **Triggers** (см. таблицу C в плане):
  - `busy_ratio` > 0.5 sustained 30s
  - `swap` > 0
  - `slow_count_60s` ≥ 3
  - `nrestarts` ++
  - `age_max_60s_sec` > 5
  - `linux_sysfs_timeouts_1h` ≥ 1
  - `knn_empty_results_1h` ≥ 1
  - `rss > 0.8 * limit`

### III. Hot-state mmap в tmpfs

**Pattern:** Linux kernel-style seqlock без блокировки чтения.

- **Path:** `$XDG_RUNTIME_DIR/coolstep/state.mmap` (mode 0600).
  Fallback: `/tmp/coolstep-$UID/state.mmap` если XDG_RUNTIME_DIR пуст.
- **Struct (packed):**
  ```python
  # coolstep/core/hot_state.py
  import struct
  HEADER = struct.Struct("<QQQ")  # seq, ts_ns, body_len
  BODY = struct.Struct("<dddddddddd")  # cpu_temp, gpu_temp, fan_max, ...
  ```
- **Writer (daemon):** `seq++; fence; write_body; fence; seq++`.
  Odd seq = write in progress; reader retries.
- **Reader (dashboard):** read seq, body, seq2. If `seq != seq2` or odd → retry.
- **Старый `ml-state.json`** не убираем — оставляем для:
  - Recovery после crash (mmap уносится с logout)
  - `/api/predictor-breakdown` (full snapshot, не hot path)
  - Запись раз в 5-10s, не каждый tick.
- **Эффект:** dump_ml_state 50-200ms на tick → mmap write <0.1ms.
  Disk I/O 95% уходит.
- **Безопасность:**
  - Per-user mode 0600
  - Fixed-size mmap — не растёт
  - tmpfs очищается на logout → нет persistent state leak
  - Atomic snapshot (seqlock) — нет torn reads

### IV. Убрать SSE

- **Удалить:** `dashboard/server.py:900` route `/api/sse/telemetry`.
- **Frontend:**
  - `static/components/live-telemetry-tile.js` — заменить EventSource → `setInterval(1000)` + `fetch('/api/telemetry/latest')`.
  - То же для других SSE consumers (если есть).
- **Hypothesis:** часть heap-роста у dashboard (1.6G anon) — это long-lived
  SSE connections + Starlette streaming buffers. После IV — re-measure.
- **Trade-off:** теряем «push»-семантику, но её и не было (SSE was 1Hz polling).
  Получаем простоту + меньше open FD.
- **ADR-008 update:** добавить раздел «SSE → polling rollback 2026-05-X»
  в `docs/stack-decisions.md`.

### V. Shadow-service A/B (B.3 в исходном плане)

- **Файл:** `~/.config/systemd/user/coolstep-shadow.service`
- **Env:**
  ```
  COOLSTEP_ACTUATOR_ENABLE=false
  COOLSTEP_HOME=%h/.local/state/coolstep-shadow
  ```
- **Run:** 24h рядом с armed коллектором.
- **Compare:** residual distributions armed vs. shadow. Если они **одинаковые** —
  actuator ничего не даёт. Если **shadow shifted** — actuator работает.
- **Independent:** можно делать после I-IV или параллельно (отдельный путь данных).

## Implementation order (ROI × dependencies)

| # | Step | Effort | ROI | Depends |
|---|------|--------|-----|---------|
| 1 | I — busy_ratio | XS | high (foundation) | — |
| 2 | II — self-monitor tile | M | high (visibility) | I |
| 3 | III — hot-state mmap | M | high (kills dump_ml_state lag) | — |
| 4 | IV — SSE → polling | S | medium (heap probe) | — |
| 5 | V — shadow service | S | medium (calibration only) | — |

I→II последовательно, остальное может идти parallel в worktree.

## Out of scope (документировано отдельно)

- Real dashboard heap leak ([[project-coolstep-dashboard-heap-leak]]) —
  ожидается что IV частично разрулит; полная investigation отдельно.
- Chroma SEGV/empty result mitigation — отдельный путь
  ([[project-coolstep-p2-8-memory-layers]]).
- Predictor phase-bucket fix — независимо
  ([[project-coolstep-predictor-phase-bucket]]).

## Links

- Incident trigger: AGE 8s 2026-05-14, см. memory `[[project-coolstep-dashboard-heap-leak]]`
- Current mitigations: drop-ins `20-memory.conf` (collector 1.5G), `10-memory.conf` (dashboard 3G), оба + `MemorySwapMax`
- Architecture invariant: `coolstep/dashboard/CLAUDE.md` «No state in process»
- Stack ADR: `docs/stack-decisions.md` ADR-008 (SSE choice)
