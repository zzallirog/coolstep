"""AMD GPU (amdgpu kernel driver) collector via sysfs.

Reads:
- /sys/class/drm/card*/device/hwmon/hwmon*/* — temp/power/freq/in (voltage)
- /sys/class/drm/card*/device/pp_dpm_sclk и mclk — current state markers (*)
- /sys/class/drm/card*/device/power_dpm_force_performance_level — current mode

Each `card*` becomes one GpuMetrics. Both iGPU (Radeon 780M на target) и dGPU
(если AMD discrete) обрабатываются единообразно.
"""

from __future__ import annotations

import re
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, GpuMetrics, SignalDescriptor

CURRENT_DPM_RE = re.compile(r"^(\d+):\s*(\d+)Mhz\s*\*\s*$", re.MULTILINE)


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


def _parse_current_dpm(content: str) -> float | None:
    m = CURRENT_DPM_RE.search(content)
    if not m:
        return None
    try:
        return float(m.group(2))
    except ValueError:
        return None


class AmdGpuCollector:
    name = "amdgpu"

    def __init__(self, drm_root: Path = Path("/sys/class/drm")) -> None:
        self.drm_root = drm_root
        self._cost = CostTracker()
        self._cards: list[Path] = []

    def discover(self) -> bool:
        if not self.drm_root.is_dir():
            return False
        cards: list[Path] = []
        for card in sorted(self.drm_root.glob("card[0-9]*")):
            if "-" in card.name:
                # card0-DP-1 — это connector, не device
                continue
            device = card / "device"
            uevent = _read_text(device / "uevent") or ""
            if "amdgpu" in uevent.lower() or "DRIVER=amdgpu" in uevent:
                cards.append(card)
        self._cards = cards
        return bool(cards)

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="gpu.amd.temp_c", unit="°C", dtype="float", cardinality="per-device",
                source="/sys/class/drm/cardN/device/hwmon/hwmonM/temp1_input",
                requires=("amdgpu kernel module",),
            ),
            SignalDescriptor(
                name="gpu.amd.power_w", unit="W", dtype="float", cardinality="per-device",
                source="/sys/class/drm/cardN/device/hwmon/hwmonM/power1_average",
            ),
            SignalDescriptor(
                name="gpu.amd.voltage_v", unit="V", dtype="float", cardinality="per-device",
                source="/sys/class/drm/cardN/device/hwmon/hwmonM/in0_input",
            ),
            SignalDescriptor(
                name="gpu.amd.sclk_mhz", unit="MHz", dtype="float", cardinality="per-device",
                source="/sys/class/drm/cardN/device/pp_dpm_sclk (current state '*')",
            ),
            SignalDescriptor(
                name="gpu.amd.mclk_mhz", unit="MHz", dtype="float", cardinality="per-device",
                source="/sys/class/drm/cardN/device/pp_dpm_mclk (current state '*')",
            ),
            SignalDescriptor(
                name="gpu.amd.util_pct", unit="%", dtype="float", cardinality="per-device",
                source="/sys/class/drm/cardN/device/gpu_busy_percent",
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            gpus = [self._sample_card(card) for card in self._cards]
        self._cost.record_us(sw.elapsed_us)
        return {"gpus": gpus}

    def _sample_card(self, card: Path) -> GpuMetrics:
        device = card / "device"
        name = _read_card_name(device) or f"amdgpu_{card.name}"

        temp_c: float | None = None
        power_w: float | None = None
        voltage_v: float | None = None
        for hwmon_dir in (device / "hwmon").glob("hwmon*"):
            if temp_c is None:
                t_raw = _read_int(hwmon_dir / "temp1_input")
                if t_raw is not None:
                    temp_c = t_raw / 1000.0
            if power_w is None:
                p_raw = _read_int(hwmon_dir / "power1_average")
                if p_raw is not None:
                    power_w = p_raw / 1_000_000.0
            if voltage_v is None:
                v_raw = _read_int(hwmon_dir / "in0_input")
                if v_raw is not None:
                    voltage_v = v_raw / 1000.0

        sclk = None
        sclk_text = _read_text(device / "pp_dpm_sclk")
        if sclk_text:
            sclk = _parse_current_dpm(sclk_text + "\n")
        mclk = None
        mclk_text = _read_text(device / "pp_dpm_mclk")
        if mclk_text:
            mclk = _parse_current_dpm(mclk_text + "\n")

        util_pct = None
        gpu_busy = _read_text(device / "gpu_busy_percent")
        if gpu_busy is not None:
            try:
                util_pct = float(gpu_busy)
            except ValueError:
                util_pct = None

        return GpuMetrics(
            name=name,
            temp_c=temp_c,
            power_w=power_w,
            sclk_mhz=sclk,
            mclk_mhz=mclk,
            util_pct=util_pct,
            voltage_v=voltage_v,
        )


def _read_card_name(device: Path) -> str | None:
    """Try a few known device-id locations; fall back to vendor-product symlink."""
    product = _read_text(device / "product")
    if product:
        return product
    for marker in ("subsystem_device", "device"):
        val = _read_text(device / marker)
        if val:
            return f"amdgpu_{val}"
    return None


def make() -> AmdGpuCollector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not _c.gpu_amd:
        return None
    collector = AmdGpuCollector()
    return collector if collector.discover() else None
