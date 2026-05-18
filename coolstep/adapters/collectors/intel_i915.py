"""Intel iGPU collector — i915 / xe driver via sysfs.

Targets Intel integrated GPUs (UHD, Iris Xe, Arc).  Reads:
- /sys/class/drm/card*/gt/gt0/rps_act_freq_mhz   — actual current freq
- /sys/class/drm/card*/gt/gt0/rps_cur_freq_mhz   — requested freq
- /sys/class/drm/card*/gt/gt0/rps_max_freq_mhz   — boost ceiling
- /sys/class/drm/card*/gt/gt0/throttle_reason_*  — boolean flags
- /sys/class/drm/card*/device/hwmon/hwmon*/      — temp_input, power*_input

Both `i915` (legacy) and `xe` (new) drivers expose the same `gt/` layout.
Older `i915` kernels expose `gt_act_freq_mhz` directly under `card*/` — handled
as a fallback.

Each Intel card produces one GpuMetrics with name="intel_<card>".
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, GpuMetrics, SignalDescriptor

log = logging.getLogger(__name__)

_DRM_ROOT = Path("/sys/class/drm")
_INTEL_VENDOR = "0x8086"


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _intel_cards(drm_root: Path) -> list[Path]:
    """Return card directories whose vendor ID is Intel (0x8086)."""
    cards: list[Path] = []
    if not drm_root.is_dir():
        return cards
    for card in sorted(drm_root.iterdir()):
        if not re.match(r"card\d+$", card.name):
            continue
        vendor = _read_text(card / "device" / "vendor")
        if vendor == _INTEL_VENDOR:
            cards.append(card)
    return cards


def _read_card_freq_mhz(card: Path) -> float | None:
    """Find the actual GPU frequency.  Try new gt/gt0/ first, then legacy."""
    # New layout (i915 4.16+, xe): card*/gt/gt0/rps_act_freq_mhz
    new_paths = [
        card / "gt" / "gt0" / "rps_act_freq_mhz",
        card / "gt" / "gt0" / "rps_cur_freq_mhz",
    ]
    # Legacy: card*/gt_act_freq_mhz
    legacy_paths = [
        card / "gt_act_freq_mhz",
        card / "gt_cur_freq_mhz",
    ]
    for p in new_paths + legacy_paths:
        v = _read_int(p)
        if v is not None and v > 0:
            return float(v)
    return None


def _read_throttle_reasons(card: Path) -> dict[str, bool]:
    """Return a flat dict of throttle_reason_* flags from gt0/."""
    flags: dict[str, bool] = {}
    gt0 = card / "gt" / "gt0"
    if not gt0.is_dir():
        return flags
    for f in gt0.glob("throttle_reason_*"):
        val = _read_int(f)
        if val is not None:
            # Strip "throttle_reason_" prefix
            key = f.name.replace("throttle_reason_", "")
            flags[key] = bool(val)
    return flags


def _read_hwmon_temp_power(card: Path) -> tuple[float | None, float | None]:
    """Locate hwmon dir under card and return (temp_c, power_w)."""
    hwmon_root = card / "device" / "hwmon"
    if not hwmon_root.is_dir():
        return None, None
    for hwmon in hwmon_root.iterdir():
        temp_input = hwmon / "temp1_input"
        power_input = hwmon / "power1_input"
        temp = _read_int(temp_input)
        power = _read_int(power_input)
        # temp in milli-°C, power in µW (per hwmon convention)
        temp_c = temp / 1000.0 if temp is not None else None
        power_w = power / 1_000_000.0 if power is not None else None
        if temp_c is not None or power_w is not None:
            return temp_c, power_w
    return None, None


class IntelI915Collector:
    name = "intel_i915"

    def __init__(self, drm_root: Path = _DRM_ROOT) -> None:
        self._drm_root = drm_root
        self._cost = CostTracker()
        self._cards: list[Path] = []

    def discover(self) -> bool:
        self._cards = _intel_cards(self._drm_root)
        return bool(self._cards)

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="gpu.temp_c", unit="°C", dtype="float",
                source="/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input",
                cardinality="per-device",
                description="Intel iGPU die temperature",
                requires=("i915 or xe driver",),
            ),
            SignalDescriptor(
                name="gpu.sclk_mhz", unit="MHz", dtype="float",
                source="/sys/class/drm/card*/gt/gt0/rps_act_freq_mhz",
                cardinality="per-device",
                description="Intel iGPU current render clock",
                requires=("i915 or xe driver",),
            ),
            SignalDescriptor(
                name="gpu.power_w", unit="W", dtype="float",
                source="/sys/class/drm/card*/device/hwmon/hwmon*/power1_input",
                cardinality="per-device",
                description="Intel iGPU package power",
                requires=("i915 or xe driver",),
            ),
            SignalDescriptor(
                name="gpu.throttle_reasons", unit="bool", dtype="dict[str, bool]",
                source="/sys/class/drm/card*/gt/gt0/throttle_reason_*",
                cardinality="dict",
                description="Throttle cause flags (thermal/power/voltage limits)",
                requires=("i915 or xe driver",),
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            gpus: list[GpuMetrics] = []
            throttle_all: dict[str, bool] = {}
            for card in self._cards:
                freq = _read_card_freq_mhz(card)
                temp, power = _read_hwmon_temp_power(card)
                name = f"intel_{card.name}"
                gpus.append(GpuMetrics(
                    name=name,
                    temp_c=temp,
                    power_w=power,
                    sclk_mhz=freq,
                ))
                throttle = _read_throttle_reasons(card)
                for k, v in throttle.items():
                    throttle_all[f"{card.name}_{k}"] = v

            partial: dict[str, object] = {}
            if gpus:
                partial["gpus"] = gpus
            if throttle_all:
                # Surface in platform_state as compact string flags
                partial["platform_state"] = {
                    f"intel_throttle.{k}": "1" if v else "0"
                    for k, v in throttle_all.items()
                }
            self._cost.record_us(sw.elapsed_us)
            return partial


def make() -> IntelI915Collector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not _c.gpu_intel:
        return None
    collector = IntelI915Collector()
    return collector if collector.discover() else None
