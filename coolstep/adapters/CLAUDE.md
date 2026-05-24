# CLAUDE.md — `coolstep/adapters/`

> Per-platform слой. Здесь и только здесь живут subprocess'ы, sysfs reads,
> внешние SDK, vendor commands.

**Module version:** 0.1.0
**Last synced with master:** 2026-05-24
**Connectors:**
- ↑ package → `../CLAUDE.md`
- ↓ children → `collectors/CLAUDE.md`, `actuators/CLAUDE.md`
- ← consumed by → `daemon.py`, `dashboard/server.py`

## Purpose

Адаптеры разделены по направлению потока данных:
- **collectors/** — телеметрия в систему (sysfs/SDK → TelemetryFrame partial)
- **actuators/** — действия наружу (Action → vendor cmd)

Каждый adapter — модуль с `make() -> Adapter | None`. Реестр (`__init__.py`)
обходит sibling-модули, импортирует, зовёт make(). None = adapter не
работает на этом хосте → молча пропускается.

## Invariants

- **`make()` обязателен** в каждом adapter-модуле, кроме `_base.py`.
- **`make()` возвращает None при отсутствии железа/команды/permissions** —
  не падает с exception. Exception = баг adapter'а.
- **Adapter не падает на ошибке sample/apply** — должен вернуть partial dict
  (для collector) либо ActionResult с error (для actuator).
- **mypy strict не требуется** — здесь живут SDK с плохими stubs (pynvml,
  asusctl wrap). Ruff обязателен везде.
- **Не пишем напрямую в hwmon/MSR** для actuators. Всегда обёртка над
  существующими CLI-тулзами (asusctl, ryzenadj, nvidia-smi, ipmitool).
  Reasoning → `../../docs/stack-decisions.md` ADR-010.

## Public surface

```python
from coolstep.adapters.collectors import discover as discover_collectors
from coolstep.adapters.actuators import discover as discover_actuators
from coolstep.adapters.collectors._base import Collector, CostTracker, stopwatch
from coolstep.adapters.actuators._base import Actuator
```

## Visit when

- пишешь новый adapter → этот файл + `collectors/CLAUDE.md` или `actuators/CLAUDE.md`
- меняешь Collector/Actuator Protocol → core/schema.py, dashboard, daemon
- добавляешь новую платформу → `../../docs/platform-support-matrix.md`

## FAQ

**Q: Где template для нового collector?**
A: → `collectors/CLAUDE.md` секция «Template». Минимум 30 LOC, не больше.

**Q: Adapter может звать другой adapter?**
A: Нет. Они независимы. Если один collector нужен другому — это знак, что
надо вынести общий код в `_base.py` либо в `core/`.

**Q: Что такое `_base.py`?**
A: Helpers, общие для adapters одного типа. `_base.py` НЕ загружается registry
(см. `_SKIP_MODULES`). Всё, что начинается с `_`, registry скипает.

**Q: Откуда берутся costs (sample_us)?**
A: Adapter использует `stopwatch()` контекст-менеджер из `_base.py` в
`sample()`/`apply()`. Это catches wall time. Кладёт в `CostTracker` rolling
window. `cost()` отдаёт avg.

## Sibling pointers

- → `collectors/CLAUDE.md` — как пишутся collectors, что есть сейчас
- → `actuators/CLAUDE.md` — как пишутся actuators, safety rules для hardware writes
- → `../core/CLAUDE.md` — данные, которые adapter'ы наполняют/читают
- → `../../docs/platform-support-matrix.md` — где adapter'ы есть/нет
