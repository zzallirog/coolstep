# CLAUDE.md — `coolstep/core/`

> Platform-neutral ядро. Никаких subprocess'ов, никаких sysfs reads, никаких
> SDK. Только данные и логика над данными.

**Module version:** 0.1.0
**Last synced with master:** 2026-05-24
**Connectors:**
- ↑ package → `../CLAUDE.md`
- ← consumed by → `adapters/`, `dashboard/`, `inspect/`, `daemon.py`

## Purpose

Контракты (`schema.py`), in-memory ring (`ring.py`), персистенс (`store.py`),
извлечение признаков (`fingerprint.py`), интерфейс предиктора + baseline
(`predictor.py`), генерация Action'ов (`decision.py`), оценка готовности
(`calibration.py`).

## Invariants

- **mypy strict** обязателен (см. `pyproject.toml`). Это foundation, типы
  не размываем.
- **Никаких внешних зависимостей** кроме stdlib. Если нужна numpy/pandas —
  это P1+ artefact, не в core.
- **Datatypes immutable наружу** — caller не должен полагаться на mutation.
  Внутри метода dataclass можно мутировать, но возврат — новая ссылка либо
  результат-объект.
- **schema.py** — единственное место, где живут TelemetryFrame/Action/Cost.
  Adapter возвращает `dict[str, object]` с partial fields; merge_partial()
  склеивает. Никогда не возвращай TelemetryFrame целиком из adapter — это
  работа ядра.

## Public surface

```python
from coolstep.core.schema import (
    TelemetryFrame, CpuMetrics, GpuMetrics, FanMetrics, ProcSig,
    WorkloadFrame, Cost, Action, ActionVerb, ActionResult, SimResult,
    merge_partial,
)
from coolstep.core.ring import Ring
from coolstep.core.store import Store, FRAMES_TTL_SEC, EVENTS_TTL_SEC
from coolstep.core.fingerprint import extract
from coolstep.core.predictor import Predictor, Prediction, AlwaysIdleBaseline
from coolstep.core.decision import DecisionEngine, Thresholds
from coolstep.core.calibration import evaluate, GateResult, CalibrationReport
```

## Visit when

- добавляешь поле в TelemetryFrame → обязан обновить `merge_partial()`,
  `Store.write_frame()`, `docs/telemetry-schema.md`, mock-data в tests
- меняешь signature Predictor/Actuator Protocol → bump version + sync master + check adapters/
- меняешь sqlite schema → миграция (P0 пока нет; подумать о ALTER в P1)
- добавляешь calibration gate → docs/calibration-gates.md + dashboard tile

## FAQ

**Q: Почему ring.py отдельно от store.py?**
A: ring живёт в памяти (low-latency для predictor), store на диске (для replay,
training, audit). Два разных времени жизни — разные модули.

**Q: Почему fingerprint.py не в schema.py?**
A: schema хранит данные, fingerprint выводит признаки. Разные слои:
schema — "что есть", fingerprint — "что мы из этого вычисляем".

**Q: AlwaysIdleBaseline зачем?**
A: P0 placeholder. Pipeline должен крутиться end-to-end до того, как появится
real ML model. Заменяется на XGBoostPredictor в P1 без изменения интерфейса.

**Q: Где actuator interface?**
A: `coolstep/adapters/actuators/_base.py:Actuator`. Core определяет данные
(Action / SimResult / ActionResult в schema.py), а контракт actuator'а — на
стороне adapters, потому что это уже platform-edge.

## Sibling pointers

- → `../adapters/collectors/CLAUDE.md` — кто заполняет TelemetryFrame
- → `../adapters/actuators/CLAUDE.md` — кто потребляет Action'ы
- → `../dashboard/CLAUDE.md` — кто рендерит CalibrationReport / Prediction
- → `../../docs/architecture.md` — full-picture контекст
