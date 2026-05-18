"""nvidia_nvml collector tests with a fake pynvml module."""

from __future__ import annotations

from unittest.mock import MagicMock

from coolstep.adapters.collectors.nvidia_nvml import NvidiaNvmlCollector


class _FakeUtil:
    def __init__(self, gpu: int) -> None:
        self.gpu = gpu


def _make_fake_pynvml(devices: list[dict[str, object]]) -> MagicMock:
    fake = MagicMock()
    fake.nvmlInit = MagicMock(return_value=None)
    fake.nvmlDeviceGetCount = MagicMock(return_value=len(devices))

    handles = list(range(len(devices)))
    fake.nvmlDeviceGetHandleByIndex.side_effect = lambda idx: handles[idx]
    fake.nvmlDeviceGetName.side_effect = lambda h: devices[h]["name"]
    fake.nvmlDeviceGetTemperature.side_effect = lambda h, sensor: devices[h]["temp"]
    fake.nvmlDeviceGetPowerUsage.side_effect = lambda h: devices[h]["power_mw"]
    fake.nvmlDeviceGetClockInfo.side_effect = lambda h, kind: (
        devices[h]["sclk"] if kind == 0 else devices[h]["mclk"]
    )
    fake.nvmlDeviceGetUtilizationRates.side_effect = lambda h: _FakeUtil(devices[h]["util"])
    return fake


def test_discover_no_devices_returns_false():
    fake = _make_fake_pynvml([])
    assert NvidiaNvmlCollector(fake).discover() is False


def test_discover_with_one_device_returns_true():
    fake = _make_fake_pynvml(
        [{"name": b"NVIDIA GeForce RTX 4060", "temp": 40, "power_mw": 13050, "sclk": 630, "mclk": 7001, "util": 12}]
    )
    assert NvidiaNvmlCollector(fake).discover() is True


def test_sample_translates_units():
    fake = _make_fake_pynvml(
        [{"name": b"RTX 4060", "temp": 45, "power_mw": 25000, "sclk": 1500, "mclk": 7001, "util": 50}]
    )
    c = NvidiaNvmlCollector(fake)
    assert c.discover()
    partial = c.sample()
    gpus = partial["gpus"]
    assert isinstance(gpus, list)
    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "RTX 4060"
    assert g.temp_c == 45.0
    assert g.power_w == 25.0  # 25000 mW → 25 W
    assert g.sclk_mhz == 1500.0
    assert g.util_pct == 50.0


def test_sample_handles_individual_failures():
    """Если один из getters падает — остальные поля заполняются, упавший = None."""
    fake = _make_fake_pynvml(
        [{"name": b"RTX 4060", "temp": 45, "power_mw": 25000, "sclk": 1500, "mclk": 7001, "util": 50}]
    )

    def boom(*_args):
        raise RuntimeError("nvml panic")

    fake.nvmlDeviceGetPowerUsage.side_effect = boom

    c = NvidiaNvmlCollector(fake)
    assert c.discover()
    partial = c.sample()
    gpus = partial["gpus"]
    assert isinstance(gpus, list)
    assert len(gpus) == 1
    assert gpus[0].temp_c == 45.0
    assert gpus[0].power_w is None


def test_init_failure_propagates_to_discover_false():
    fake = MagicMock()
    fake.nvmlInit.side_effect = RuntimeError("driver missing")
    c = NvidiaNvmlCollector(fake)
    assert c.discover() is False


def test_sample_before_discover_returns_empty():
    fake = _make_fake_pynvml(
        [{"name": b"RTX 4060", "temp": 45, "power_mw": 25000, "sclk": 1500, "mclk": 7001, "util": 50}]
    )
    c = NvidiaNvmlCollector(fake)
    # discover() намеренно не вызываем
    assert c.sample() == {"gpus": []}
