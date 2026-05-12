"""ACPI / generic thermal_zone collector — primary thermal source on ARM.

Reads `/sys/class/thermal/thermal_zone*/temp` (milli-°C) and the
companion `type` file describing what each zone represents:

    thermal_zone0/type  -> "cpu-thermal"  | "acpitz" | "x86_pkg_temp" | ...
    thermal_zone0/temp  -> 47000          (47.000 °C)

Standard ACPI interface — works on:
- ARM SoCs (Raspberry Pi, NVIDIA Jetson, Apple Silicon Asahi, AWS Graviton)
- Older x86 laptops without dedicated hwmon drivers
- Cloud VMs (limited zones, but enough for KNN signal)

On x86 where `linux_sysfs` already discovers k10temp / coretemp via hwmon,
this collector still registers — its zones (CPU, ambient, battery) provide
redundant cross-checks and fill `cpu.temps_c` with zone names that hwmon
doesn't expose (e.g. "x86_pkg_temp" — sometimes only thermal_zone has it).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, SignalDescriptor

log = logging.getLogger(__name__)

_THERMAL_ROOT = Path("/sys/class/thermal")
_ZONE_RE = re.compile(r"^thermal_zone\d+$")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class _Zone:
    __slots__ = ("path", "type")

    def __init__(self, path: Path, ztype: str) -> None:
        self.path = path
        self.type = ztype


class ArmThermalCollector:
    name = "arm_thermal"

    def __init__(self, root: Path = _THERMAL_ROOT) -> None:
        self._root = root
        self._cost = CostTracker()
        self._zones: list[_Zone] = []

    def discover(self) -> bool:
        self._zones = []
        if not self._root.is_dir():
            return False
        for zone_dir in sorted(self._root.iterdir()):
            if not _ZONE_RE.match(zone_dir.name):
                continue
            ztype = _read_text(zone_dir / "type") or zone_dir.name
            temp_p = zone_dir / "temp"
            if temp_p.exists():
                self._zones.append(_Zone(zone_dir, ztype))
        return bool(self._zones)

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="cpu.temps_c",
                unit="°C",
                dtype="dict[str, float]",
                source="/sys/class/thermal/thermal_zone*/temp",
                cardinality="dict",
                description="ACPI thermal zones — primary source on ARM",
                requires=("ACPI thermal subsystem",),
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            temps: dict[str, float] = {}
            for z in self._zones:
                val = _read_int(z.path / "temp")
                if val is None:
                    continue
                # Filter implausible readings (some kernels report -1)
                if val <= 0 or val > 200_000:
                    log.debug("arm_thermal: dropped implausible %s = %d m°C", z.type, val)
                    continue
                # Use zone type as key; suffix with index when types collide
                key = z.type
                if key in temps:
                    key = f"{z.type}_{z.path.name.replace('thermal_zone', '')}"
                temps[key] = val / 1000.0

            partial: dict[str, object] = {}
            if temps:
                partial["cpu"] = {"temps_c": temps}
            self._cost.record_us(sw.elapsed_us)
            return partial


def make() -> ArmThermalCollector | None:
    # Intentional: no caps_if_set() gate here. ACPI thermal zones are present
    # on every modern Linux host regardless of arch, CPU vendor, or compositor,
    # and the collector's own discover() handles the "no zones" case cleanly.
    # Gating on caps would add ceremony without filtering anything new.
    collector = ArmThermalCollector()
    return collector if collector.discover() else None
