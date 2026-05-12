"""NVIDIA GPU collector via NVML (pynvml).

Cross-platform (Linux/Windows). discover() succeeds only if pynvml is installed
AND at least one device is detected. Each device contributes one GpuMetrics
entry.
"""

from __future__ import annotations

from typing import Any

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, GpuMetrics, SignalDescriptor


class NvidiaNvmlCollector:
    name = "nvidia_nvml"

    def __init__(self, pynvml_module: Any) -> None:
        self._nvml = pynvml_module
        self._cost = CostTracker()
        self._handles: list[Any] = []
        self._device_names: list[str] = []
        self._initialized = False

    def discover(self) -> bool:
        if self._initialized:
            return bool(self._handles)
        try:
            self._nvml.nvmlInit()
            count = int(self._nvml.nvmlDeviceGetCount())
        except Exception:  # noqa: BLE001
            return False
        if count <= 0:
            return False
        self._handles = [self._nvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
        self._device_names = []
        for h in self._handles:
            try:
                raw = self._nvml.nvmlDeviceGetName(h)
                name = raw.decode() if isinstance(raw, bytes) else str(raw)
            except Exception:  # noqa: BLE001
                name = "nvidia_unknown"
            self._device_names.append(name)
        self._initialized = True
        return True

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="gpu.nvidia.temp_c", unit="°C", dtype="float", cardinality="per-device",
                source="NVML nvmlDeviceGetTemperature(GPU)",
                requires=("nvidia-ml-py", "NVIDIA driver"),
            ),
            SignalDescriptor(
                name="gpu.nvidia.power_w", unit="W", dtype="float", cardinality="per-device",
                source="NVML nvmlDeviceGetPowerUsage / 1000",
            ),
            SignalDescriptor(
                name="gpu.nvidia.sclk_mhz", unit="MHz", dtype="float", cardinality="per-device",
                source="NVML nvmlDeviceGetClockInfo(GRAPHICS)",
            ),
            SignalDescriptor(
                name="gpu.nvidia.mclk_mhz", unit="MHz", dtype="float", cardinality="per-device",
                source="NVML nvmlDeviceGetClockInfo(MEM)",
            ),
            SignalDescriptor(
                name="gpu.nvidia.util_pct", unit="%", dtype="float", cardinality="per-device",
                source="NVML nvmlDeviceGetUtilizationRates.gpu",
            ),
        ]

    def sample(self) -> dict[str, object]:
        if not self._initialized:
            return {"gpus": []}
        with stopwatch() as sw:
            gpus = self._sample_each()
        self._cost.record_us(sw.elapsed_us)
        return {"gpus": gpus}

    def _sample_each(self) -> list[GpuMetrics]:
        out: list[GpuMetrics] = []
        nvml = self._nvml
        for handle, name in zip(self._handles, self._device_names, strict=True):
            temp_raw = _safe(nvml.nvmlDeviceGetTemperature, handle, 0)
            temp = float(temp_raw) if temp_raw is not None else None
            power_mw = _safe(nvml.nvmlDeviceGetPowerUsage, handle)
            power = power_mw / 1000.0 if power_mw is not None else None
            sclk_raw = _safe(nvml.nvmlDeviceGetClockInfo, handle, 0)
            sclk = float(sclk_raw) if sclk_raw is not None else None
            mclk_raw = _safe(nvml.nvmlDeviceGetClockInfo, handle, 2)
            mclk = float(mclk_raw) if mclk_raw is not None else None
            util_obj = _safe(nvml.nvmlDeviceGetUtilizationRates, handle)
            util_pct: float | None = None
            if util_obj is not None and hasattr(util_obj, "gpu"):
                util_pct = float(util_obj.gpu)
            out.append(
                GpuMetrics(
                    name=name,
                    temp_c=temp,
                    power_w=power,
                    sclk_mhz=sclk,
                    mclk_mhz=mclk,
                    util_pct=util_pct,
                )
            )
        return out


def _safe(fn, *args):  # type: ignore[no-untyped-def]
    """Call NVML function defensively. Returns None on any exception.

    Принимает callable + positional args вместо lambda с capture'ом — иначе
    `for handle in ...: lambda: f(handle)` ловит B023 (loop-var binding)
    и легко ломается при refactor.
    """
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return None


def make() -> NvidiaNvmlCollector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not _c.gpu_nvidia:
        return None
    try:
        import pynvml  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    collector = NvidiaNvmlCollector(pynvml)
    return collector if collector.discover() else None
