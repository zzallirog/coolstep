# CLAUDE.md — `coolstep/` package root

> Точка входа в Python-пакет. Тут живёт `daemon.py`, всё остальное — подмодули.

**Module version:** 0.3.0
**Last synced with master:** 2026-05-13
**Connectors:**
- ↑ master → `~/coolstep/CLAUDE.md`
- ↓ children → `core/CLAUDE.md`, `adapters/CLAUDE.md`, `dashboard/CLAUDE.md`, `inspect/CLAUDE.md`

## Purpose

Собирает дерево модулей в один Python-пакет с четырьмя entry points (`coolstep`,
`coolstep-collector`, `coolstep-dashboard`, `coolstep-mcp`). Сам по себе ничего не делает —
делегирует.

## Invariants

- Версия здесь живёт в `__init__.py:__version__`. Bumpнули — sync в master CLAUDE.md.
- Никакой бизнес-логики в этом уровне. Только импорты + entry-points.

## Public surface

```python
from coolstep import __version__
from coolstep.daemon import Daemon, run_collector
```

## Visit when

- меняешь имя пакета или entry-points
- bumpнул __version__
- добавил новый top-level subdir

## FAQ

**Q: Куда положить новый модуль уровня package root?**
A: Только если он реально platform-neutral и не вписывается в core/adapters/dashboard/inspect. В 95% случаев — в один из существующих.

**Q: Где daemon main loop?**
A: `daemon.py` в этом directory. Он читает collectors из `adapters/collectors/`, actuators из `adapters/actuators/`.

## Sibling pointers

- → `core/CLAUDE.md` — schema, ring, store, fingerprint, predictor, decision, calibration
- → `adapters/CLAUDE.md` — общий контракт adapter'ов + per-platform реализации
- → `dashboard/CLAUDE.md` — FastAPI бэкенд + frontend artifacts
- → `inspect/CLAUDE.md` — CLI subcommands
