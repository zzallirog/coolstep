"""Fingerprint feature extraction tests."""

from __future__ import annotations

from coolstep.core.fingerprint import extract
from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    TelemetryFrame,
    WorkloadFrame,
)


def _frame(ts: float, *, load: list[float], tctl: float = 70.0, gpu_temp: float = 50.0,
           fan: int = 2000, power_pkg: float | None = None) -> TelemetryFrame:
    cpu = CpuMetrics(load_pct=load, temps_c={"tctl": tctl})
    if power_pkg is not None:
        cpu.power_w["package"] = power_pkg
    return TelemetryFrame(
        timestamp=ts,
        cpu=cpu,
        gpus=[GpuMetrics(name="g", temp_c=gpu_temp)],
        fans=[FanMetrics(name="cpu_fan", rpm=fan)],
    )


def test_extract_empty_window_returns_empty():
    assert extract([]) == {}


def test_extract_basic_features():
    window = [
        _frame(1.0, load=[10.0, 12.0], tctl=70.0),
        _frame(2.0, load=[20.0, 22.0], tctl=72.0),
        _frame(3.0, load=[85.0, 88.0], tctl=80.0),
    ]
    f = extract(window)
    assert f["cpu_load_max"] > 80.0
    assert "cpu_load_avg" in f
    assert "cpu_load_p95" in f
    assert f["peak_count"] == 1.0
    assert f["cpu_temp_max"] == 80.0
    assert "cpu_temp_slope_per_sec" in f
    assert f["cpu_temp_slope_per_sec"] > 0  # rising
    assert f["spike_density"] > 0


def test_extract_carries_workload_features():
    frame = _frame(1.0, load=[10.0])
    frame.workload = WorkloadFrame(rolling_features={"visible_apps": 5.0, "unique_classes": 3.0})
    f = extract([frame])
    assert f["visible_apps"] == 5.0
    assert f["unique_classes"] == 3.0


def test_extract_handles_missing_gpu_or_fan():
    frame = TelemetryFrame(
        timestamp=1.0,
        cpu=CpuMetrics(load_pct=[10.0], temps_c={"tctl": 70.0}),
    )
    f = extract([frame])
    assert "gpu_temp_max" not in f
    assert "fan_rpm_max" not in f
    assert f["cpu_load_max"] == 10.0


def test_extract_slope_zero_for_constant_temp():
    window = [
        _frame(1.0, load=[10.0], tctl=70.0),
        _frame(2.0, load=[10.0], tctl=70.0),
        _frame(3.0, load=[10.0], tctl=70.0),
    ]
    f = extract(window)
    assert f["cpu_temp_slope_per_sec"] == 0.0


def test_extract_no_temp_data_skips_temp_features():
    frame = TelemetryFrame(
        timestamp=1.0,
        cpu=CpuMetrics(load_pct=[10.0]),
    )
    f = extract([frame])
    assert "cpu_temp_max" not in f
    assert "cpu_temp_slope_per_sec" not in f


# ── P2.5 Phase D: heat_soak_index ──────────────────────────────────────


def test_heat_soak_index_uses_power_when_available():
    """When power_pkg moves meaningfully, ΔT/ΔPower is the primary signal."""
    window = [
        _frame(0.0, load=[10.0], tctl=60.0, power_pkg=10.0),
        _frame(1.0, load=[10.0], tctl=62.0, power_pkg=20.0),
        _frame(2.0, load=[10.0], tctl=64.0, power_pkg=30.0),
    ]
    f = extract(window)
    assert "heat_soak_index" in f
    # ΔT ≈ 2 °C/s, ΔPower ≈ 10 W/s → ratio ≈ 0.2.
    assert 0.15 < f["heat_soak_index"] < 0.25


def test_heat_soak_index_falls_back_to_load():
    """When ΔPower is tiny (≤ 1 W/s) load slope acts as the proxy."""
    window = [
        _frame(0.0, load=[10.0], tctl=60.0, power_pkg=0.0),
        _frame(1.0, load=[40.0], tctl=62.0, power_pkg=0.0),
        _frame(2.0, load=[70.0], tctl=64.0, power_pkg=0.0),
    ]
    f = extract(window)
    assert "heat_soak_index" in f
    # ΔT ≈ 2 °C/s, Δload ≈ 30 %/s → ratio ≈ 0.0667.
    assert 0.05 < f["heat_soak_index"] < 0.08


def test_heat_soak_index_zero_when_no_movement():
    """No power change, no load change → return 0.0 rather than divide-by-zero."""
    window = [
        _frame(0.0, load=[10.0], tctl=60.0, power_pkg=0.0),
        _frame(1.0, load=[10.0], tctl=62.0, power_pkg=0.0),
        _frame(2.0, load=[10.0], tctl=64.0, power_pkg=0.0),
    ]
    f = extract(window)
    assert f["heat_soak_index"] == 0.0


def test_heat_soak_index_skipped_when_no_temp_data():
    """If we don't have a temp slope to divide, the feature is absent."""
    frame = TelemetryFrame(
        timestamp=1.0,
        cpu=CpuMetrics(load_pct=[10.0]),
    )
    f = extract([frame])
    assert "heat_soak_index" not in f


# ── v3 phase-bucket: cpu_temp_avg_5min ─────────────────────────────────


def test_cpu_temp_avg_5min_uses_only_last_300_seconds():
    """Frames older than `latest_ts − 300 s` must not pull the 5-min mean
    toward an old regime. Construct an old-cold + recent-hot trajectory:
    the 5-min mean should reflect the recent-hot frames only."""
    window = [
        _frame(0.0,   load=[10.0], tctl=50.0),    # old, outside 5-min
        _frame(100.0, load=[10.0], tctl=50.0),    # old, outside 5-min
        _frame(600.0, load=[80.0], tctl=78.0),    # latest_ts; recent
        _frame(700.0, load=[80.0], tctl=80.0),    # latest_ts; recent
        _frame(800.0, load=[80.0], tctl=82.0),    # latest_ts
    ]
    f = extract(window)
    assert "cpu_temp_avg_5min" in f
    # Cutoff = 800 − 300 = 500; only the three 78/80/82 frames qualify.
    assert abs(f["cpu_temp_avg_5min"] - 80.0) < 1e-6


def test_cpu_temp_avg_5min_absent_when_single_frame():
    """Need ≥ 2 frames inside the 5-min cutoff; fewer than that, leave
    the feature out so `quantise_temp_phase` falls back to plateau."""
    window = [_frame(1.0, load=[10.0], tctl=70.0)]
    f = extract(window)
    assert "cpu_temp_avg_5min" not in f
