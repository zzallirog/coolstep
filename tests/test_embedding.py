"""Embedder tests — robust scaler + L2 norm + cold start."""

from __future__ import annotations

import math

from coolstep.core.embedding import FEATURE_NAMES, Embedder, extract_raw
from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    TelemetryFrame,
    WorkloadFrame,
)


def _frame(ts: float, *, tctl: float = 70.0, freq: float = 4000.0,
           load: float = 20.0) -> TelemetryFrame:
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(
            freq_mhz=[freq] * 4,
            load_pct=[load] * 4,
            temps_c={"tctl": tctl},
            power_w={"package": 25.0},
        ),
        gpus=[GpuMetrics(name="g", temp_c=50.0, power_w=15.0, sclk_mhz=800.0, util_pct=10.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=2900)],
        platform_state={"epp": "performance", "platform_profile": "balanced", "boost": "1",
                        "governor": "performance"},
        workload=WorkloadFrame(rolling_features={"visible_apps": 3.0, "unique_classes": 2.0}),
    )


def test_extract_raw_returns_all_feature_names():
    raw = extract_raw(_frame(1.0))
    assert set(raw.keys()) == set(FEATURE_NAMES)


def test_extract_raw_categorical_indicators():
    raw = extract_raw(_frame(1.0))
    assert raw["epp_performance"] == 1.0
    assert raw["epp_balance_power"] == 0.0
    assert raw["profile_balanced"] == 1.0
    assert raw["profile_quiet"] == 0.0
    assert raw["boost_on"] == 1.0
    assert raw["governor_perf"] == 1.0


def test_embed_returns_none_before_fit():
    emb = Embedder(min_frames_to_fit=10)
    assert emb.embed(_frame(1.0)) is None
    assert not emb.fitted


def test_refit_with_fewer_than_min_does_not_fit():
    emb = Embedder(min_frames_to_fit=10)
    emb.refit([_frame(t) for t in range(5)])
    assert not emb.fitted


def test_refit_then_embed_returns_unit_norm():
    emb = Embedder(min_frames_to_fit=10)
    emb.refit([_frame(float(t), tctl=60.0 + t, freq=3500 + t * 50, load=10 + t * 5)
               for t in range(20)])
    assert emb.fitted
    vec = emb.embed(_frame(100.0, tctl=85.0))
    assert vec is not None
    assert len(vec) == len(FEATURE_NAMES)
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6


def test_outlier_does_not_blow_up_normalization():
    """Robust scaler ignores outlier — vector still finite + normed."""
    frames = [_frame(float(t), tctl=70.0) for t in range(20)]
    frames.append(_frame(99.0, tctl=200.0))  # ridiculous outlier
    emb = Embedder(min_frames_to_fit=10)
    emb.refit(frames)
    vec = emb.embed(_frame(100.0, tctl=70.0))
    assert vec is not None
    assert all(math.isfinite(x) for x in vec)


def test_stats_snapshot_serializable():
    emb = Embedder(min_frames_to_fit=10)
    emb.refit([_frame(t) for t in range(15)])
    snap = emb.stats_snapshot()
    assert set(snap.keys()) == set(FEATURE_NAMES)
    for s in snap.values():
        assert "median" in s and "mad" in s
