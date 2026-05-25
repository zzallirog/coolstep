# CLAUDE.md — `data/`

> Runtime persistence. Не коммитится в git (`.gitignore`).

**Module version:** 0.1.0
**Last synced with master:** 2026-05-23

## Layout (runtime)

```
data/
├── store.db           # sqlite WAL, написана daemon'ом (frames + throttle_events + actions)
├── store.db-wal       # write-ahead log
├── store.db-shm       # shared memory
├── ml-state.json      # daemon снапшот каждые 30 ticks (последний predict + features + costs)
├── exports/           # ручной CLI export (csv/json) — gitignored
└── models/            # обученные модели (P1+) — gitignored
```

## Invariants

- **Никогда не commit'ится.** Даже `models/` после P1.
- **Не источник truth для конфига.** Только runtime state. Если что-то в
  data/ сломалось — `rm -rf data/* && systemctl --user restart coolstep-collector`.
- **`COOLSTEP_HOME` env переопределяет путь.** По умолчанию `~/coolstep/data/`.
  Useful для tests (tmp_path), Windows/macOS (`%LOCALAPPDATA%`,
  `~/Library/Application Support/`).

## TTL

- `frames` — 14 дней (Store.rotate каждые 600 ticks)
- `throttle_events` — 90 дней
- `actions` — 90 дней (P2+)
- `ml-state.json` — overwrite каждые 30 ticks
- `models/` — keep last N версий, prune старее (P1+)

## Disk-budget

- 14 дней × 1Hz × ~500B/row (с raw_json) ≈ **600 MB** worst case
- Если sqlite растёт быстрее — индексы / VACUUM / downsampling старых дней до 10s grid
- Dashboard `Calibration state` tile следит за `coverage_seconds` и сравнивает
  с физическим размером файла

## FAQ

**Q: Backup?**
A: P0 — не нужен. Per-host data, не critical, потеря = переоткалибровываемся.
Для P3+ multi-host обсудим.

**Q: Concurrent access dashboard + daemon + cli?**
A: Sqlite WAL поддерживает много reader'ов + один writer (daemon). CLI и
dashboard — readers, проблем нет. Если запустишь два daemon'а — будет race
на write_frame; не делай так.

## Sibling pointers

- → `../coolstep/core/CLAUDE.md` — Store schema, rotation logic
- → `../docs/telemetry-schema.md` — formal column descriptions
