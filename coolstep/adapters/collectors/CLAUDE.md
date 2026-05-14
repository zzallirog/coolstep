# CLAUDE.md — `coolstep/adapters/collectors/`

> Sources of telemetry. Каждый файл — один collector. P0 на target: 4 active.

**Module version:** 0.1.0
**Last synced with master:** 2026-05-03
**Connectors:**
- ↑ adapters → `../CLAUDE.md`
- ↔ peer → `../actuators/CLAUDE.md`
- ← consumed by → `daemon.py`, `inspect/cli.py:adapters`, `dashboard/server.py:/api/adapters`

## Active collectors (P0)

| File | Reads | Status on target | Cost ms | Signals |
|---|---|---|---|---|
| `linux_sysfs.py` | hwmon, cpufreq, /proc/stat, platform_state | ✅ | ~18 | 12 |
| `nvidia_nvml.py` | NVML (через nvidia-ml-py) | ✅ | ~0.05 | 5 |
| `amdgpu.py` | /sys/class/drm/card*/device/* | ✅ | ~1 | 6 |
| `hyprctl.py` | subprocess hyprctl clients -j (5s cache) | ✅ | ~0.2 (cached) / ~95 (refresh) | 3 |

Total: **26 discovered signals**. Manifest на `/api/discoveries`.

## Invariants

- Файл `_base.py` — общие helpers (`Collector` Protocol, `CostTracker`, `stopwatch`).
  Registry его не подбирает (см. `__init__.py:_SKIP_MODULES`).
- Каждый collector экспортирует `make() -> Collector | None`.
- Каждый collector имеет атрибут `name: str` — uniq, snake_case.
- `discover()` идемпотентен — повторный вызов возвращает тот же результат.
  (Регрессия из Sprint E: nvidia_nvml аккумулировал handles.)
- `sample()` returns `dict[str, object]` с partial fields из TelemetryFrame.
  Не возвращает TelemetryFrame целиком — это работа `merge_partial()` в core.
- `cost()` measures sample latency через rolling window. Никогда не врёт.
- **`signals()` обязателен** — Zabbix-LLD-style manifest. Каждый эмиттируемый
  сигнал → один `SignalDescriptor(name, unit, dtype, source, ...)`. Dashboard
  и research-tools читают это вместо хардкода имён полей.

## Template для нового collector

```python
# coolstep/adapters/collectors/my_platform.py
from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost


class MyPlatformCollector:
    name = "my_platform"

    def __init__(self) -> None:
        self._cost = CostTracker()

    def discover(self) -> bool:
        # check that hardware/cmd/permission exists; idempotent
        return True

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        # Zabbix-LLD-style manifest: каждый эмиттируемый сигнал
        return [
            SignalDescriptor(
                name="cpu.freq_mhz", unit="MHz", dtype="list[float]",
                cardinality="per-core",
                source="my-platform-specific-source",
                requires=("kernel_module_X",),
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            partial: dict[str, object] = {
                "cpu": {"freq_mhz": [...]},
                # ... whatever this platform provides
            }
        self._cost.record_us(sw.elapsed_us)
        return partial


def make() -> MyPlatformCollector | None:
    c = MyPlatformCollector()
    return c if c.discover() else None
```

Тесты — `tests/adapters/test_my_platform.py` с fakeroot tmp_path / mocked SDK.

## Visit when

- пишешь новый collector → этот файл (добавить в Active table) + tests
- адаптер падает в production → `discover()` логика проверена через graceful
  fallback (никогда raise)
- появляется requirement новых полей TelemetryFrame → также `core/schema.py`
  + `core/store.py:write_frame` shortcut columns

## FAQ

**Q: Почему linux_sysfs пропускает amdgpu hwmon?**
A: Потому что есть отдельный `amdgpu.py` collector со специфическими полями
(pp_dpm_sclk/mclk regex, voltage). Двойная запись в TelemetryFrame.gpus = bug.

**Q: Что делать, если discover() требует sleep (warm-up driver)?**
A: Не делать sleep в discover. Это registry-level check, должен быть быстрый.
Warm-up — внутри `make()` или ленивый при первом `sample()`.

**Q: Как интегрировать hyprctl на не-Hyprland WM?**
A: Не интегрировать. Создать новый collector `wmctrl.py` или `swaymsg.py`.
hyprctl.py останется Hyprland-specific.

**Q: Где cost budget gate?**
A: `core/calibration.py:cost_cpu/cost_rss` gates. Daemon должен передать
total cost туда.

## Sibling pointers

- → `../actuators/CLAUDE.md` — кто потребляет данные через decision pipeline
- → `../../core/CLAUDE.md` — schema TelemetryFrame, merge_partial
- → `../../../docs/telemetry-schema.md` — что собирается + sqlite schema
- → `../../../docs/platform-support-matrix.md` — что планируется
