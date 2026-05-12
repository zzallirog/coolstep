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
