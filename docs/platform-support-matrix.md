# Platform support matrix

Где coolstep уже работает / куда планируется. Ядро (`coolstep/core/`) —
платформо-нейтральное; адаптеры — per-platform.

## Текущая (P0)

| Платформа | Collectors готовы | Actuators готовы | Status |
|---|---|---|---|
| **Linux laptop** (target: ASUS TUF A15, AMD+NVIDIA) | linux_sysfs, amdgpu, nvidia_nvml, hyprctl (4 из 12 shipped collectors — полный список в `coolstep/adapters/collectors/CLAUDE.md`) | readonly_log | ✅ live, все listed discover'ятся, 98 tests (P0-era count) |
| Linux desktop (без iGPU/без Hyprland) | linux_sysfs, nvidia_nvml | readonly_log | ⚠️ должно работать (linux_sysfs универсален), не verified |
| **Linux headless server** (Intel i5-13500 / Debian 13 / kernel 6.12) | linux_sysfs, intel_i915, dbus_session, rapl_energy | readonly_log | ✅ live since 2026-05-12, 5/5 discovers, headless via linger |
| Windows | none | none | ❌ |
| macOS | none | none | ❌ |
| BMC/IPMI server | none | none | ❌ |

### Measured `linux_sysfs` sample latency

Steady-state cost over a 200-tick window on production hosts. Daemon
loop = 1 Hz (`coolstep-collector.service --period 1.0`).

| Host | Topology | median | p95 | Source |
|---|---|---|---|---|
| ASUS TUF A15 (Ryzen 7940HS, 16 logical) | k10temp (4 inputs) + asus fans + nvme | ~16 ms | — | `docs/p2.5-rollup.md` |
| Intel i5-13500 server (20 logical) | coretemp (15 inputs) + 2× nvme + acpitz + asus | 19.7 ms | 20.5 ms | profiled 2026-05-23 (issue #5) |

Bimodal distribution on Intel comes from selective-refresh schedule
(`FREQ_REFRESH_TICKS=3` + `VOLTAGE_REFRESH_TICKS=5`): «fast» ticks ~12 ms,
«slow» ticks (freq+voltage refresh together) ~20 ms. Acceptable —
budget is `<100 ms` per sample.

## Планируемая

### P3 — Linux desktop

Добавить:
- `linux_perf` collector — perf events (cycles, IPC, cache-miss) через
  `subprocess.Popen perf stat`
- `wmctrl` / `swaymsg` collector — workload context на не-Hyprland WMs
- Verify linux_sysfs на Intel (k10temp → coretemp + RAPL).

### P4 — Server

Добавить:
- `redfish` collector — HTTPS REST к BMC, температуры/мощности через стандарт
  Redfish (DMTF)
- `ipmi` collector fallback — `subprocess ipmitool sdr`
- `cgroup` workload context — per-cgroup CPU/memory/IO как замена hyprctl

### P5 — Windows

Добавить:
- `windows_lhm` collector — LibreHardwareMonitor через subprocess (HTTP API
  если запущен как сервис, или CLI)
- `windows_powerplan` actuator — `powercfg` для EPP-эквивалентных шагов
- Service framework: NSSM или нативный Windows Service

### P6 — macOS

Добавить:
- `macos_powermetrics` collector — `subprocess powermetrics --samplers
  cpu_power,gpu_power,smc` (требует sudo, есть workaround через `pmcli`)
- `macos_iokit` collector — temperature через PyObjC IOKit bindings
- `macos_pmset` actuator — управление профилями через pmset

## Roadmap по приоритету

```
P0 (now)  ──▶  Linux laptop, 4 collectors, readonly actuator, dashboard backend
P1        ──▶  ML predictor (xgboost), real fingerprint clustering, predictions live
P2        ──▶  Linux soft actuators (asusctl/ryzenadj/EPP) под флагом + sandbox-first
P3        ──▶  Linux desktop verification + perf collector
P4        ──▶  Server: Redfish/IPMI + cgroup workload
P5        ──▶  Windows: LHM + powercfg
P6        ──▶  macOS: powermetrics + pmset
P7+       ──▶  RL для actuator policy / federated learning (cross-host)
```

## Adapter добавляется без правок core

Реестр (`coolstep/adapters/{collectors,actuators}/__init__.py`) обходит
sibling-модули и вызывает `make()`. Возврат None = adapter не подходит для
этого хоста. Один новый файл = один новый adapter, никаких other файлов
менять не нужно.

Минимальный template нового collector'а:

```python
# coolstep/adapters/collectors/my_platform.py
from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost

class MyCollector:
    name = "my_platform"

    def __init__(self) -> None:
        self._cost = CostTracker()

    def discover(self) -> bool:
        return True  # real check: import / env / device files

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def sample(self) -> dict:
        with stopwatch() as sw:
            partial = {"cpu": {"freq_mhz": [...]}}  # whatever you can get
        self._cost.record_us(sw.elapsed_us)
        return partial


def make() -> MyCollector | None:
    c = MyCollector()
    return c if c.discover() else None
```

Тот же шаблон для actuator (`adapters/actuators/`), только `dry_run` /
`apply` / `revert` методы.

## Поддерживаемые actions

| Action | Linux | Windows | macOS | Server |
|---|---|---|---|---|
| `notify_user` | notify-send | toast | NSUserNotification | log only |
| `ramp_cooling` | asusctl fan-curve | LHM-fancontrol | smcFanControl | IPMI fan |
| `cap_boost` | EPP shift | powercfg | pmset | n/a |
| `shift_power_envelope` | EPP+governor | powercfg | pmset | RAPL |
| `defer_workload` | cgroups freeze | Job objects | renice/launchctl | cgroups |

Выбор actuator'а делает router в decision flow: первый supports() = используется.
