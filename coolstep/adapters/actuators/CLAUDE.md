# CLAUDE.md — `coolstep/adapters/actuators/`

> Sinks of decisions. Action verbs → vendor commands. **Безопасность —
> главная invariant.**

**Module version:** 0.2.0
**Last synced with master:** 2026-05-23
**Connectors:**
- ↑ adapters → `../CLAUDE.md`
- ↔ peer → `../collectors/CLAUDE.md`
- ← consumed by → `daemon.py` (через decision engine)

## Active actuators (P2 — armed)

| File | Verbs supported | Status |
|---|---|---|
| `readonly.py` | ALL (logs intent only) | ✅ default, always first in router |
| `notify_send.py` | NOTIFY_USER | ✅ libnotify wrapper, DBus session required |
| `asusctl_fan_curve.py` | RAMP_COOLING | ✅ asusctl wrap, ASUS laptops |
| `ryzenadj_cap.py` | CAP_BOOST | ✅ ryzenadj wrap, AMD Ryzen mobile |
| `epp_shift.py` | SHIFT_POWER_ENVELOPE | ✅ amd-pstate-epp / intel_pstate EPP |
| `game_mode_optimizer.py` | DEFER_WORKLOAD | ✅ co-operative bias when game-mode active |

Все hardware actuators **dry-run по умолчанию** — `apply()` пишет в actuator
journal, но реальная команда не выполняется, пока `COOLSTEP_ACTUATOR_ENABLE=true`
не выставлен в systemd unit и не прошли все 8 calibration gates.

## Invariants

- **`readonly_log` всегда первый в роутере.** Если пользователь не enable'ил
  hardware-actuators — все Action'ы должны идти в readonly. Это safe default.
- **Каждый hardware-actuator имеет `expires_at` revert.** Если daemon упал —
  hardware вернётся в baseline через timeout. Никаких permanent state changes.
- **`apply()` неблокирующий быстрый.** Долгие subprocess — в `asyncio.to_thread`.
- **`dry_run()` обязательно реализован** — даёт SimResult без записи в hardware.
  Используется dashboard'ом и pre-deploy smoke.
- **`revert()` идемпотентен** — может вызываться много раз без negative side
  effects.
- **Не пишем напрямую в hwmon/MSR/DAC.** Обёртка над:
  - Linux: `asusctl`, `ryzenadj`, `nvidia-smi`, `cpupower`, `tlp` (если есть)
  - Windows: `powercfg`, LibreHardwareMonitor
  - macOS: `pmset`, `smcFanControl`
  - Server: `ipmitool`, Redfish HTTPS
  Reasoning → `../../../docs/stack-decisions.md` ADR-010.
- **Action audit обязателен** — в P2+ каждый apply() пишет в `actions` table
  через `core/store.py`.

## Visit when

- пишешь новый actuator → этот файл (Active table) + safety review (ADR-010)
- меняешь Actuator Protocol → core/decision.py + dashboard
- появляется новый ActionVerb → core/schema.py + decision.py + maybe new
  actuators

## Template для нового actuator

```python
# coolstep/adapters/actuators/asusctl_fan_curve.py  (P2 пример)
import subprocess
from coolstep.adapters.actuators._base import Actuator
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult


class AsusctlFanCurve:
    name = "asusctl_fan_curve"

    def supports(self, verb: ActionVerb) -> bool:
        return verb == ActionVerb.RAMP_COOLING

    def dry_run(self, action: Action) -> SimResult:
        # симуляция: считаем ожидаемое temp снижение по empirical model
        return SimResult(
            expected_effect={"cpu_temp_delta_c": -3.0},
            confidence=0.7,
            reverts_in=action.params.get("duration_sec", 30.0),
        )

    def apply(self, action: Action) -> ActionResult:
        # asusctl fan-curve --mod-profile Performance --fan cpu --data ...
        try:
            cp = subprocess.run([...], check=True, capture_output=True, timeout=2)
            return ActionResult(applied_at=time.time(),
                                cmd_executed=" ".join(cp.args),
                                stdout_tail=cp.stdout[-200:].decode())
        except subprocess.CalledProcessError as exc:
            return ActionResult(applied_at=time.time(), cmd_executed=None,
                                error=str(exc))

    def revert(self) -> None:
        # вернуть на baseline профиль; идемпотентно
        ...


def make() -> AsusctlFanCurve | None:
    if not shutil.which("asusctl"):
        return None
    return AsusctlFanCurve()
```

## FAQ

**Q: Что если hardware actuator конфликтует с game-mode.service?**
A: Probe + skip. `actuator.supports(verb)` должен возвращать False, если
`systemctl is-active game-mode.service` — `coolstep` не наступает на
существующую mode-orchestration.

**Q: Как тестировать apply() без записи в hardware?**
A: Mock subprocess.run. tests/adapters/test_*_actuator.py фикстуры с
`patch("subprocess.run")`.

**Q: Кто решает, какой actuator получает Action?**
A: Daemon (`daemon.py:_tick`). Простой first-match роутер: `for actuator in
self.actuators: if actuator.supports(verb): apply(); break`. В P2 будет
priority routing — readonly_log параллельно для audit + один real actuator.

**Q: А если все actuators скажут supports=False?**
A: Action молча drop'ается. Это ОК — означает, что на этой платформе нет
способа выполнить. Daemon логирует warning.

## Sibling pointers

- → `../collectors/CLAUDE.md` — кто наполняет TelemetryFrame
- → `../../core/CLAUDE.md` — Action / ActionVerb / SimResult / ActionResult schema
- → `../../core/decision.py` — кто генерирует Action'ы
- → `../../../docs/stack-decisions.md` ADR-010 — почему обёртки, не direct write
