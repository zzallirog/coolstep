"""Schema dataclass + merge_partial tests."""

from __future__ import annotations

from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    ProcSig,
    TelemetryFrame,
    WorkloadFrame,
    merge_partial,
)


def test_telemetry_frame_defaults_are_empty() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    assert frame.cpu.freq_mhz == []
    assert frame.gpus == []
    assert frame.fans == []
    assert frame.workload is None


def test_merge_partial_cpu_block() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    merge_partial(
        frame,
        {
            "cpu": {
                "freq_mhz": [3000.0, 3100.0],
                "load_pct": [12.0, 8.0],
                "temps_c": {"tctl": 78.4},
                "voltage_v": {"vcore": 1.245},
                "power_w": {"package": 45.2},
            }
        },
    )
    assert frame.cpu.freq_mhz == [3000.0, 3100.0]
    assert frame.cpu.load_pct == [12.0, 8.0]
    assert frame.cpu.temps_c == {"tctl": 78.4}
    assert frame.cpu.voltage_v == {"vcore": 1.245}
    assert frame.cpu.power_w == {"package": 45.2}


def test_merge_partial_gpus_extend() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    merge_partial(frame, {"gpus": [GpuMetrics(name="a", temp_c=40.0)]})
    merge_partial(frame, {"gpus": [GpuMetrics(name="b", temp_c=50.0)]})
    assert [g.name for g in frame.gpus] == ["a", "b"]


def test_merge_partial_fans_extend() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    merge_partial(
        frame,
        {"fans": [FanMetrics(name="cpu_fan", rpm=2900), FanMetrics(name="gpu_fan", rpm=2900)]},
    )
    assert len(frame.fans) == 2
    assert frame.fans[0].rpm == 2900


def test_merge_partial_storage_temps_update() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    frame.storage_temps_c["nvme0"] = 30.0
    merge_partial(frame, {"storage_temps_c": {"nvme1": 32.0}})
    assert frame.storage_temps_c == {"nvme0": 30.0, "nvme1": 32.0}


def test_merge_partial_workload_replaces() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    wl = WorkloadFrame(top_processes=[ProcSig(pid=1, name="x", cpu_pct=50.0, rss_mb=100.0)])
    merge_partial(frame, {"workload": wl})
    assert frame.workload is wl


def test_merge_partial_platform_state_update() -> None:
    frame = TelemetryFrame(timestamp=1.0)
    merge_partial(frame, {"platform_state": {"profile": "performance"}})
    merge_partial(frame, {"platform_state": {"epp": "performance"}})
    assert frame.platform_state == {"profile": "performance", "epp": "performance"}


def test_cpu_metrics_default_factory_independence() -> None:
    """Регрессия — два CpuMetrics не должны делить mutable defaults."""
    a = CpuMetrics()
    b = CpuMetrics()
    a.freq_mhz.append(1000.0)
    assert b.freq_mhz == []
