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


# --- Variant 1: slope-disagreement blend -----------------------------------

def test_slope_blend_agreement_returns_raw_expected():
    """When short and long slopes agree (Δ < 1.5°C/s), KNN's raw forecast
    passes through unchanged — variant 1 only fires on disagreement."""
    from coolstep.core.predictor import _slope_blended_expected
    features = {
        "cpu_temp_now": 70.0,
        "cpu_temp_slope_per_sec_short": 0.5,
        "cpu_temp_slope_per_sec": 0.3,
    }
    blended, reason = _slope_blended_expected(features, raw_expected=85.0)
    assert blended is None
    assert reason is None


def test_slope_blend_cooling_dissent_pulls_expected_down():
    """The operator-flagged failure: KNN predicts 93°C peak (hot neighbours
    in 30s), but short slope shows the chip cooling at −2.8°C/s while
    long slope still reports the dying ramp at +0.8°C/s. The blend
    pulls the forecast toward the physics estimate."""
    from coolstep.core.predictor import _slope_blended_expected
    features = {
        "cpu_temp_now": 72.6,
        "cpu_temp_slope_per_sec_short": -2.8,
        "cpu_temp_slope_per_sec": 0.81,
    }
    blended, reason = _slope_blended_expected(features, raw_expected=93.5)
    assert blended is not None
    assert reason is not None
    # Δ = 3.61 → weight_short = (3.61-1.5)/3 = 0.70
    # physics_est = 72.6 + (-2.8)*4*(1-exp(-30/4)) ≈ 72.6 - 11.19 = 61.4°C
    # blended = 93.5*0.30 + 61.4*0.70 = 28.05 + 43.0 = 71.0°C
    assert 68.0 <= blended <= 74.0


def test_slope_blend_weight_caps_at_max():
    """Even at extreme slope disagreement (Δ ≥ 4.5°C/s), weight_short
    saturates at 0.8 — we never *fully* override KNN with a slope-only
    estimate, because the slope can flap on sensor jitter."""
    from coolstep.core.predictor import _slope_blended_expected
    features = {
        "cpu_temp_now": 70.0,
        "cpu_temp_slope_per_sec_short": -5.0,
        "cpu_temp_slope_per_sec": +3.0,
    }
    blended, _ = _slope_blended_expected(features, raw_expected=90.0)
    # Δ = 8.0 → weight = 0.8 (capped). short clamped to -3.0.
    # physics = 70 + (-3)*4*0.9994 ≈ 70 - 11.99 = 58.0°C
    # blended = 90*0.2 + 58*0.8 = 18 + 46.4 = 64.4°C
    assert 62.0 <= blended <= 66.5


def test_slope_blend_missing_short_slope_skips():
    """If short_slope feature isn't available (cold-start window), variant 1
    must skip rather than fall through to long_slope — the whole point is
    that the two disagree, and we can't detect disagreement with one."""
    from coolstep.core.predictor import _slope_blended_expected
    features = {
        "cpu_temp_now": 70.0,
        "cpu_temp_slope_per_sec": 0.8,
    }
    blended, _ = _slope_blended_expected(features, raw_expected=90.0)
    assert blended is None


def test_slope_blend_none_expected_skips():
    """Defensive: raw_expected=None (KNN cold or no labeled neighbours)
    must not crash the blend — return None up the stack."""
    from coolstep.core.predictor import _slope_blended_expected
    features = {
        "cpu_temp_now": 70.0,
        "cpu_temp_slope_per_sec_short": -2.5,
        "cpu_temp_slope_per_sec": 0.5,
    }
    blended, _ = _slope_blended_expected(features, raw_expected=None)
    assert blended is None
