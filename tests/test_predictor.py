"""Predictor baseline tests."""

from __future__ import annotations

from coolstep.core.predictor import AlwaysIdleBaseline


def test_always_idle_returns_zero_throttle_prob():
    p = AlwaysIdleBaseline()
    pred = p.predict({"cpu_temp_max": 75.0}, [])
    assert pred.throttle_prob == 0.0
    assert pred.confidence == 1.0
    assert pred.model_name == "always_idle_baseline"
    assert pred.expected_temp_c == 75.0


def test_always_idle_records_features_used():
    p = AlwaysIdleBaseline()
    feats = {"cpu_load_avg": 50.0, "cpu_temp_max": 80.0}
    pred = p.predict(feats, [])
    assert sorted(pred.features_used) == ["cpu_load_avg", "cpu_temp_max"]


def test_always_idle_no_features_no_temp():
    p = AlwaysIdleBaseline()
    pred = p.predict({}, [])
    assert pred.expected_temp_c is None


# ---------- TrajectoryBaseline ----------

from coolstep.core.predictor import TrajectoryBaseline


def _trajectory_predicted(current: float, slope: float) -> float:
    """Recompute the saturation-aware expected_temp_c the same way the
    predictor does — keeps tests anchored to the formula, not magic
    numbers."""
    import math
    p = TrajectoryBaseline()
    slope_clamped = max(-p.max_abs_slope, min(p.max_abs_slope, slope))
    factor = 1.0 - math.exp(-p.horizon_sec / p.tau_sec)
    return max(20.0, min(120.0, current + slope_clamped * p.tau_sec * factor))


def test_trajectory_baseline_extrapolates_rising_slope_with_saturation():
    p = TrajectoryBaseline()
    # current 60°C, slope +1°C/s.  Naive linear would say 65°C at h=5.
    # Saturation: 60 + 1·4·(1-exp(-5/4)) = 60 + 2.85 ≈ 62.85°C.
    pred = p.predict({"cpu_temp_max": 60.0, "cpu_temp_slope_per_sec": 1.0}, [])
    assert abs(pred.expected_temp_c - _trajectory_predicted(60.0, 1.0)) < 1e-6
    assert 60.0 < pred.expected_temp_c < 65.0  # bounded by saturation
    assert pred.model_name == "trajectory_baseline"
    assert pred.confidence == 0.5
    assert pred.throttle_prob == 0.0


def test_trajectory_baseline_flat_matches_current():
    p = TrajectoryBaseline()
    pred = p.predict({"cpu_temp_max": 55.0, "cpu_temp_slope_per_sec": 0.0}, [])
    assert pred.expected_temp_c == 55.0


def test_trajectory_baseline_no_temp_returns_none():
    p = TrajectoryBaseline()
    pred = p.predict({}, [])
    assert pred.expected_temp_c is None


def test_trajectory_baseline_clamps_runaway_slope_positive():
    # Sensor flap: slope says +50°C/sec → clamped to max_abs_slope.
    p = TrajectoryBaseline()
    pred = p.predict({"cpu_temp_max": 60.0, "cpu_temp_slope_per_sec": 50.0}, [])
    # With clamp + saturation: 60 + 3·4·(1-exp(-5/4)) ≈ 68.55, not 310°C.
    assert abs(pred.expected_temp_c - _trajectory_predicted(60.0, 3.0)) < 1e-6
    assert pred.expected_temp_c < 75.0


def test_trajectory_baseline_clamps_runaway_slope_negative():
    p = TrajectoryBaseline()
    pred = p.predict({"cpu_temp_max": 60.0, "cpu_temp_slope_per_sec": -50.0}, [])
    assert abs(pred.expected_temp_c - _trajectory_predicted(60.0, -3.0)) < 1e-6
    assert pred.expected_temp_c > 50.0  # not -190°C


def test_trajectory_baseline_output_clamped_to_silicon_envelope():
    p = TrajectoryBaseline()
    pred = p.predict({"cpu_temp_max": 119.0, "cpu_temp_slope_per_sec": 3.0}, [])
    assert pred.expected_temp_c == 120.0
    pred = p.predict({"cpu_temp_max": 21.0, "cpu_temp_slope_per_sec": -3.0}, [])
    assert pred.expected_temp_c == 20.0
