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


def test_bucket_of_full_features():
    f = {
        "cpu_load_slope_per_sec": -2.0,    # falling
        "cpu_temp_slope_per_sec": -0.1,    # neg
        "cpu_temp_accel_per_sec_sq": -0.05,  # decel
        "cpu_temp_max": 65.0,              # warm band
    }
    assert bucket_of(f) == (-1, -1, -1, 1)


def test_bucket_of_partial_features_uses_defaults():
    # Empty dict → defaults: (0, 0, 0, 1)
    assert bucket_of({}) == (0, 0, 0, 1)


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
