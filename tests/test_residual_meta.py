"""Meta-predictor math: bucketing, Welford stats, log transform, CUSUM, log-odds."""

from __future__ import annotations

import math

import pytest

from coolstep.core.residual_meta import (
    Cusum,
    ResidualBank,
    RunningStat,
    bucket_of,
    compose_confidence,
    confidence_to_log_odds,
    inv_log_residual,
    log_odds_to_confidence,
    log_residual,
    quantise_accel_sign,
    quantise_load_band,
    quantise_profile_band,
    quantise_slope_sign,
    quantise_temp_phase,
)


# --- quantisation ----------------------------------------------------------

@pytest.mark.parametrize("v,expected", [
    (5.0, 1), (1.0, 1), (0.5001, 1),
    (0.5, 0), (0.0, 0), (-0.5, 0),
    (-0.5001, -1), (-3.0, -1),
    (None, 0), (float("nan"), 0),
])
def test_quantise_load_band(v, expected):
    assert quantise_load_band(v) == expected


@pytest.mark.parametrize("v,expected", [
    (1.0, 1), (0.06, 1), (0.05, 0),
    (0.0, 0), (-0.05, 0), (-0.06, -1),
    (None, 0),
])
def test_quantise_slope_sign(v, expected):
    assert quantise_slope_sign(v) == expected


@pytest.mark.parametrize("v,expected", [
    (1.0, 1), (0.03, 1), (0.02, 0),
    (0.0, 0), (-0.02, 0), (-0.03, -1),
    (None, 0),
])
def test_quantise_accel_sign(v, expected):
    assert quantise_accel_sign(v) == expected


@pytest.mark.parametrize("v,expected", [
    (40.0, 0), (59.9, 0),
    (60.0, 1), (70.0, 1), (79.9, 1),
    (80.0, 2), (90.0, 2),
    (None, 1),  # default warm — refuses to skip the dot
])
def test_quantise_profile_band(v, expected):
    assert quantise_profile_band(v) == expected


@pytest.mark.parametrize("now,avg,expected", [
    # Far above 5-min mean → ramp-up regime.
    (75.0, 60.0, 0),
    (73.0, 70.0, 0),    # +3°C exactly → boundary, ascending
    # Plateau band (|Δ| < 3°C).
    (72.0, 70.0, 1),
    (70.0, 70.0, 1),
    (68.0, 70.0, 1),    # −2°C, still plateau
    # Below 5-min mean → wind-down regime.
    (67.0, 70.0, 2),    # −3°C exactly → boundary, descending
    (50.0, 70.0, 2),
    # Missing inputs / NaN → safe plateau default.
    (None, 70.0, 1),
    (70.0, None, 1),
    (None, None, 1),
    (float("nan"), 70.0, 1),
    (70.0, float("nan"), 1),
])
def test_quantise_temp_phase(now, avg, expected):
    assert quantise_temp_phase(now, avg) == expected


def test_bucket_of_full_features():
    f = {
        "cpu_load_slope_per_sec": -2.0,    # falling
        "cpu_temp_slope_per_sec": -0.1,    # neg
        "cpu_temp_accel_per_sec_sq": -0.05,  # decel
        "cpu_temp_max": 65.0,              # warm band
        "cpu_load_max": 45.0,              # mid load band (v2 axis)
    }
    # No temp_now/avg_5min provided → phase axis (v3) defaults to plateau=1.
    assert bucket_of(f) == (-1, -1, -1, 1, 1, 1)


def test_bucket_of_partial_features_uses_defaults():
    # Empty dict → defaults: (0, 0, 0, 1, 1, 1)  (v3 added 6th axis: plateau)
    assert bucket_of({}) == (0, 0, 0, 1, 1, 1)


def test_bucket_of_v2_load_pct_band_splits_idle_from_active():
    """Regression (operator-flagged 2026-05-13): bucket (0,0,0,1) historically
    pooled idle-warm AND active-warm workloads under one key. v2's load_pct
    axis splits them so meta-correction learned under one regime no longer
    bleeds into the other."""
    base = {
        "cpu_load_slope_per_sec": 0.0,
        "cpu_temp_slope_per_sec": 0.0,
        "cpu_temp_accel_per_sec_sq": 0.0,
        "cpu_temp_max": 65.0,             # warm band, same in both
    }
    idle = {**base, "cpu_load_max": 5.0}     # idle band
    active = {**base, "cpu_load_max": 80.0}  # high band
    assert bucket_of(idle) != bucket_of(active)
    # Phase axis (v3) plateau=1 by default — no temp_now/avg_5min here.
    assert bucket_of(idle) == (0, 0, 0, 1, 0, 1)
    assert bucket_of(active) == (0, 0, 0, 1, 2, 1)


def test_bucket_of_v3_phase_splits_ascending_from_descending():
    """v3 regression (2026-05-13): the operator-flagged bucket (0,-1,1,1,0)
    pooled ramp-up and wind-down — same instantaneous (slope, accel) appears
    on both. Phase axis splits them by recent thermal history (T_now vs
    T_avg_5min) so each sub-regime accumulates its own systematic bias."""
    base = {
        "cpu_load_slope_per_sec": 0.0,
        "cpu_temp_slope_per_sec": -0.1,
        "cpu_temp_accel_per_sec_sq": +0.05,
        "cpu_temp_max": 70.0,
        "cpu_load_max": 20.0,
    }
    # Same shared axes, the only thing that moves is recent-history phase.
    ascending = {**base, "cpu_temp_now": 75.0, "cpu_temp_avg_5min": 62.0}
    descending = {**base, "cpu_temp_now": 70.0, "cpu_temp_avg_5min": 78.0}
    plateau = {**base, "cpu_temp_now": 70.0, "cpu_temp_avg_5min": 71.0}
    ka = bucket_of(ascending)
    kd = bucket_of(descending)
    kp = bucket_of(plateau)
    # First five axes identical across the trio; only phase differs.
    assert ka[:5] == kd[:5] == kp[:5]
    assert ka[5] == 0
    assert kp[5] == 1
    assert kd[5] == 2
    assert ka != kd != kp


def test_residual_bank_no_pooling_across_phases():
    """v3 regression: residuals of opposite sign observed under ascending
    vs descending sub-phases used to collapse into one bucket with mean≈0.
    With phase axis, each sub-bucket holds its own bias and `correct()`
    returns sign-distinct corrections per phase."""
    bank = ResidualBank()
    base = {
        "cpu_load_slope_per_sec": 0.0,
        "cpu_temp_slope_per_sec": -0.1,
        "cpu_temp_accel_per_sec_sq": +0.05,
        "cpu_temp_max": 70.0,
        "cpu_load_max": 20.0,
    }
    ascending = {**base, "cpu_temp_now": 75.0, "cpu_temp_avg_5min": 62.0}
    descending = {**base, "cpu_temp_now": 70.0, "cpu_temp_avg_5min": 78.0}
    # Big n so the Bayesian shrinkage (k=5) doesn't dominate the test.
    for _ in range(100):
        bank.observe(ascending, +5.0)
        bank.observe(descending, -5.0)
    c_asc, _, _ = bank.correct(ascending)
    c_desc, _, _ = bank.correct(descending)
    # Without phase splitting, the same bucket would have learned mean≈0.
    # With v3, the two phases hold opposite-sign corrections.
    assert c_asc > +2.5
    assert c_desc < -2.5
    # Bank holds two distinct keys (one per phase), not one pooled one.
    assert len(bank.stats) == 2


def test_from_log_migrates_v2_5tuple_to_v3_6tuple(tmp_path):
    """Migration v2→v3 (2026-05-13): on-disk 5-tuple records (load_pct
    axis already added, phase axis not yet) must re-bucket through
    `bucket_of(features)` so the rebuilt bank carries 6-tuple keys
    uniformly. A v2-shaped record with no temp_now/avg_5min lands in
    plateau (1) — the documented cold-start behaviour."""
    import json
    from coolstep.core.residual_log import ResidualLog

    log_path = tmp_path / "residual-state.jsonl"
    v2_record = {
        "ts": 2000.0,
        "predicted_at": 1970.0,
        "horizon_sec": 30.0,
        "predicted_temp_c": 72.0,
        "actual_temp_c": 67.0,
        "residual_c": -5.0,
        "model_name": "knn_v1+meta",
        "profile": "balanced",
        "bucket_key": [0, 0, 0, 1, 2],   # v2 5-tuple shape
        "features": {
            "cpu_load_slope_per_sec": 0.0,
            "cpu_temp_slope_per_sec": 0.0,
            "cpu_temp_accel_per_sec_sq": 0.0,
            "cpu_temp_max": 65.0,
            "cpu_load_max": 80.0,
        },
    }
    log_path.write_text(json.dumps(v2_record) + "\n")

    bank = ResidualBank.from_log(ResidualLog(log_path))

    keys = list(bank.stats.keys())
    assert keys == [(0, 0, 0, 1, 2, 1)], (
        f"expected v3 6-tuple from features re-bucket, got {keys}"
    )
    correction, _sigma, n = bank.correct({
        "cpu_load_slope_per_sec": 0.0,
        "cpu_temp_slope_per_sec": 0.0,
        "cpu_temp_accel_per_sec_sq": 0.0,
        "cpu_temp_max": 65.0,
        "cpu_load_max": 80.0,
    })
    assert n == 1
    assert correction != 0.0


# --- log_residual ----------------------------------------------------------

def test_log_residual_preserves_sign():
    assert log_residual(10.0) > 0
    assert log_residual(-10.0) < 0
    assert log_residual(0.0) == 0.0


def test_log_residual_compresses_large_values():
    """A +20 outlier should be much less than 20× a +1 residual."""
    assert log_residual(20.0) < 4.0  # actually log1p(20)=3.04
    assert log_residual(1.0) < 1.0   # log1p(1)=0.693


def test_log_residual_roundtrip():
    for r in [-25.0, -5.5, -0.1, 0.0, 0.3, 5.5, 25.0]:
        assert abs(inv_log_residual(log_residual(r)) - r) < 1e-9


def test_log_residual_handles_nan():
    assert log_residual(float("nan")) == 0.0
    assert inv_log_residual(float("inf")) == 0.0


# --- RunningStat ----------------------------------------------------------

def test_running_stat_welford_matches_naive():
    rs = RunningStat()
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    for x in xs:
        rs.observe(x)
    assert abs(rs.mean - 3.0) < 1e-9
    # variance(unbiased) = 2.5 for 1..5
    assert abs(rs.variance() - 2.5) < 1e-9
    assert abs(rs.std() - math.sqrt(2.5)) < 1e-9


def test_running_stat_ewma_warms_up_to_first_value():
    rs = RunningStat(alpha=0.1)
    rs.observe(10.0)
    assert rs.ewma_mean == 10.0  # first value seeds EWMA


def test_running_stat_ewma_recency_bias():
    rs = RunningStat(alpha=0.5)
    for _ in range(20):
        rs.observe(0.0)
    rs.observe(100.0)
    # alpha=0.5: last sample contributes 50%
    assert rs.ewma_mean == 50.0


def test_running_stat_decay_halves_count():
    rs = RunningStat()
    for x in range(10):
        rs.observe(float(x))
    n_before = rs.n
    mean_before = rs.mean
    rs.decay(0.5)
    assert abs(rs.n - n_before * 0.5) < 1e-9
    # Mean unchanged: it's *where*, not *how confident*
    assert abs(rs.mean - mean_before) < 1e-9


def test_running_stat_decay_bad_factor_is_noop():
    rs = RunningStat()
    rs.observe(1.0)
    rs.decay(-0.5)   # invalid
    assert rs.n == 1.0
    rs.decay(1.5)    # invalid
    assert rs.n == 1.0


def test_running_stat_ignores_nan():
    rs = RunningStat()
    rs.observe(1.0)
    rs.observe(float("nan"))
    rs.observe(2.0)
    assert rs.n == 2.0
    assert abs(rs.mean - 1.5) < 1e-9


# --- ResidualBank ----------------------------------------------------------

def test_residual_bank_converges_to_pure_data_at_large_n():
    """Asymptotic behaviour: at n=200 the shrinkage factor 200/(200+k=5)
    = 0.976 is close enough to 1 that the correction recovers the
    underlying −20°C systematic bias to within ~5%."""
    bank = ResidualBank()
    feat = {
        "cpu_load_slope_per_sec": -2.0,
        "cpu_temp_slope_per_sec": -0.1,
        "cpu_temp_accel_per_sec_sq": -0.05,
        "cpu_temp_max": 65.0,
    }
    for _ in range(200):
        bank.observe(feat, residual_c=-20.0)
    correction, std, n = bank.correct(feat)
    # 200 obs → shrinkage 200/205 ≈ 0.976; in log space pure ewma is
    # -log1p(20) = -3.044; shrunk log = -3.044*0.976 = -2.971;
    # inv_log = -(e^2.971 - 1) = -18.51°C.  Allow band ±1°C.
    assert -19.6 <= correction <= -17.5
    assert n == 200


def test_residual_bank_no_observation_returns_prior_view():
    """Empty bucket returns (correction=0, σ=prior_linear, n=0) — the
    dashboard renders a "no information" σ-band, not a zero-width one
    that lies about confidence."""
    bank = ResidualBank()
    correction, std, n = bank.correct({"cpu_temp_max": 50.0})
    assert correction == 0.0
    assert n == 0
    # PRIOR_LINEAR_C=2 → inv_log(log1p(2))=2.0
    assert 1.5 <= std <= 2.5


def test_residual_bank_bucket_separation():
    """Different buckets keep separate state.  Use n=100 so shrinkage
    (100/105 ≈ 0.95) doesn't dominate the asymmetry signal."""
    bank = ResidualBank()
    feat_falling = {"cpu_load_slope_per_sec": -2.0, "cpu_temp_max": 65.0}
    feat_rising = {"cpu_load_slope_per_sec": +2.0, "cpu_temp_max": 65.0}
    for _ in range(100):
        bank.observe(feat_falling, -10.0)
        bank.observe(feat_rising, +3.0)
    c1, _, _ = bank.correct(feat_falling)
    c2, _, _ = bank.correct(feat_rising)
    # -10°C true bias × shrinkage 0.95 in log space ≈ -8.4°C linear.
    assert c1 < -7.0
    assert c2 > 1.5


# --- Bayesian shrinkage ---------------------------------------------------


def test_shrinkage_damps_correction_at_small_n():
    """Operator-observed pathology: bucket with n=2 reported correction
    −6.23°C with σ=0.01°C.  Shrinkage damps both: correction smaller,
    σ wider, so dashboard doesn't draw an overconfident forecast."""
    bank = ResidualBank()
    feat = {"cpu_temp_max": 65.0}
    # Two agreeing samples — Welford σ would be 0 without shrinkage.
    bank.observe(feat, residual_c=-6.0)
    bank.observe(feat, residual_c=-6.0)
    correction, std, n = bank.correct(feat)
    assert n == 2
    # Shrinkage 2/(2+k=5)=0.286 in log space.  log_residual(-6)=−log(7)
    # ≈ −1.946; shrunk = −0.556; inv_log = −(e^0.556 − 1) = −0.74°C.
    # Allow band so the test isn't fragile to k tweaks.
    assert -1.5 <= correction <= -0.3
    # σ inflated by prior + disagreement term — well above the
    # near-zero raw Welford value.  Should be O(1°C) at minimum.
    assert std > 0.8


def test_shrinkage_widens_sigma_when_data_disagrees_with_prior():
    """When the data mean is far from the prior mean (0), the posterior
    σ widens to reflect that conflict — the operator sees a wide σ-band
    saying "either you're learning something new or this is noise"."""
    bank = ResidualBank()
    feat = {"cpu_temp_max": 65.0}
    # Strong consistent signal — both 5 agreeing samples of −10°C.
    for _ in range(5):
        bank.observe(feat, residual_c=-10.0)
    correction, std, n = bank.correct(feat)
    # Shrinkage 5/10=0.5 → correction ≈ −3.5°C, but the disagreement
    # term inflates σ well past the prior 2°C.
    assert -5.0 <= correction <= -2.0
    assert std > 1.5


def test_shrinkage_factor_monotonic_in_n():
    """The damping factor n/(n+k) is strictly monotonic in n: more
    observations ⇒ more weight on the data."""
    bank_small = ResidualBank()
    bank_large = ResidualBank()
    feat = {"cpu_temp_max": 65.0}
    for _ in range(3):
        bank_small.observe(feat, residual_c=-10.0)
    for _ in range(50):
        bank_large.observe(feat, residual_c=-10.0)
    c_small, _, _ = bank_small.correct(feat)
    c_large, _, _ = bank_large.correct(feat)
    # |c_large| must exceed |c_small| because shrinkage relaxes.
    assert abs(c_large) > abs(c_small)


def test_residual_bank_decay_all_halves_counts():
    bank = ResidualBank()
    feat = {"cpu_temp_max": 65.0}
    for _ in range(20):
        bank.observe(feat, -5.0)
    n_before = next(iter(bank.stats.values())).n
    bank.decay_all(0.5)
    n_after = next(iter(bank.stats.values())).n
    assert abs(n_after - n_before * 0.5) < 1e-9


def test_from_log_migrates_v1_4tuple_to_v2_5tuple(tmp_path):
    """Migration v1→v2 (2026-05-13): records on disk carry 4-tuple bucket_key
    from before load_pct_band was added.  from_log() must re-bucket them via
    features so the rebuilt bank uses 5-tuple keys uniformly — otherwise
    a later correct() call lands on a key shape mismatch."""
    import json
    from coolstep.core.residual_log import ResidualLog

    log_path = tmp_path / "residual-state.jsonl"
    # Synthesise a v1-shaped record: 4-tuple bucket_key + populated features.
    v1_record = {
        "ts": 1000.0,
        "predicted_at": 970.0,
        "horizon_sec": 30.0,
        "predicted_temp_c": 70.0,
        "actual_temp_c": 65.0,
        "residual_c": -5.0,
        "model_name": "knn_v1+meta",
        "profile": "balanced",
        "bucket_key": [0, 0, 0, 1],  # old 4-tuple shape
        "features": {
            "cpu_load_slope_per_sec": 0.0,
            "cpu_temp_slope_per_sec": 0.0,
            "cpu_temp_accel_per_sec_sq": 0.0,
            "cpu_temp_max": 65.0,
            "cpu_load_max": 80.0,  # high band → 5th axis = 2
        },
    }
    log_path.write_text(json.dumps(v1_record) + "\n")

    bank = ResidualBank.from_log(ResidualLog(log_path))

    # Bank rebuilt with current 6-tuple key derived from features.
    # Phase axis = 1 (plateau) because temp_now/avg_5min absent on v1.
    keys = list(bank.stats.keys())
    assert keys == [(0, 0, 0, 1, 2, 1)], (
        f"expected v3 6-tuple from features re-bucket, got {keys}"
    )
    # correct() round-trips on the new key without KeyError.
    correction, _sigma, n = bank.correct({
        "cpu_load_slope_per_sec": 0.0,
        "cpu_temp_slope_per_sec": 0.0,
        "cpu_temp_accel_per_sec_sq": 0.0,
        "cpu_temp_max": 65.0,
        "cpu_load_max": 80.0,
    })
    assert n == 1
    assert correction != 0.0


# --- Cusum ----------------------------------------------------------

def test_cusum_no_drift_no_trip():
    cu = Cusum(mu=0.0, k=0.5, h=4.0)
    for _ in range(50):
        assert cu.feed(0.1) is None
        assert cu.feed(-0.1) is None


def test_cusum_trips_on_sustained_down_shift():
    cu = Cusum(mu=0.0, k=0.5, h=4.0)
    # Sequence of negative log-residuals (mirrors the user's
    # -26, -24, -22, -20 streak in log space ≈ -3.3 each).
    trips: list[str] = []
    for _ in range(10):
        t = cu.feed(-3.0)
        if t:
            trips.append(t)
    assert "down" in trips
    # After a trip, accumulator auto-reset:
    assert cu.s_down == 0.0


def test_cusum_trips_on_sustained_up_shift():
    cu = Cusum(mu=0.0, k=0.5, h=4.0)
    trips: list[str] = []
    for _ in range(10):
        t = cu.feed(+3.0)
        if t:
            trips.append(t)
    assert "up" in trips


def test_cusum_reset_clears_both_sides():
    cu = Cusum()
    cu.feed(-2.0)
    cu.feed(-2.0)
    cu.reset()
    assert cu.s_up == 0.0
    assert cu.s_down == 0.0


# --- log-odds confidence composition ---------------------------------

def test_confidence_log_odds_roundtrip():
    for p in [0.01, 0.1, 0.5, 0.9, 0.99]:
        lo = confidence_to_log_odds(p)
        p_back = log_odds_to_confidence(lo)
        assert abs(p - p_back) < 1e-9


def test_compose_confidence_agreement_strengthens():
    """Two independent sources at 0.7 should combine above 0.7."""
    c = compose_confidence(0.7, 0.7)
    assert c > 0.7
    assert c < 1.0


def test_compose_confidence_disagreement_cancels():
    """High + low should land near the middle."""
    c = compose_confidence(0.9, 0.1)
    assert 0.4 <= c <= 0.6


def test_compose_confidence_empty_returns_neutral():
    assert compose_confidence() == 0.5
