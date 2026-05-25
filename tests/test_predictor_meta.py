"""MetaPredictor composition: base + bank → corrected Prediction."""

from __future__ import annotations

from coolstep.core.predictor import TrajectoryBaseline
from coolstep.core.predictor_meta import MetaPredictor
from coolstep.core.residual_meta import ResidualBank


def test_meta_passthrough_when_bank_empty():
    """Empty bank → correction 0; expected_temp_c matches base."""
    base = TrajectoryBaseline()
    meta = MetaPredictor(base=base, bank=ResidualBank())
    features = {"cpu_temp_max": 60.0, "cpu_temp_slope_per_sec": 0.0}
    base_pred = base.predict(features, [])
    meta_pred = meta.predict(features, [])
    assert abs(meta_pred.expected_temp_c - base_pred.expected_temp_c) < 1e-9


def test_meta_applies_learned_correction():
    """After observing 200× -10°C residuals in a bucket, the corrected
    forecast should be ~10°C lower than base.  Uses n=200 so the
    shrinkage prior (k=5) has relaxed to ~97% of pure data; the
    shrinkage is on purpose at small n — see
    `test_meta_correction_damped_at_small_n`."""
    base = TrajectoryBaseline()
    bank = ResidualBank()
    features = {
        "cpu_load_slope_per_sec": -2.0,    # falling
        "cpu_temp_slope_per_sec": -0.1,
        "cpu_temp_accel_per_sec_sq": -0.05,
        "cpu_temp_max": 65.0,
        "cpu_load_avg": 20.0,
    }
    for _ in range(200):
        bank.observe(features, residual_c=-10.0)
    meta = MetaPredictor(base=base, bank=bank)
    base_pred = base.predict(features, [])
    meta_pred = meta.predict(features, [])
    delta = meta_pred.expected_temp_c - base_pred.expected_temp_c
    assert -10.5 <= delta <= -8.5


def test_meta_correction_damped_at_small_n():
    """Companion to the shrinkage prior: at n=2 the correction is
    damped toward 0, preventing the overconfident forecast the
    operator screenshotted (n=2 bucket reporting σ=0.01°C with
    correction −6.23°C → image attached 2026-05-12)."""
    base = TrajectoryBaseline()
    bank = ResidualBank()
    features = {"cpu_temp_max": 65.0, "cpu_temp_slope_per_sec": 0.0}
    bank.observe(features, residual_c=-6.0)
    bank.observe(features, residual_c=-6.0)
    meta = MetaPredictor(base=base, bank=bank)
    base_pred = base.predict(features, [])
    meta_pred = meta.predict(features, [])
    delta = meta_pred.expected_temp_c - base_pred.expected_temp_c
    # Pre-shrinkage delta would have been ≈ −6°C; shrunk it's well
    # under −2°C — operator no longer sees a confident strong correction
    # built from two ticks of agreement.
    assert -2.0 <= delta <= 0.0


def test_meta_passes_through_when_base_returns_none():
    """If base couldn't predict (no temp), meta does nothing."""
    base = TrajectoryBaseline()
    meta = MetaPredictor(base=base, bank=ResidualBank())
    pred = meta.predict({}, [])
    assert pred.expected_temp_c is None


def test_meta_name_compositional():
    base = TrajectoryBaseline()
    meta = MetaPredictor(base=base, bank=ResidualBank())
    assert meta.name == "trajectory_baseline+meta"


def test_meta_mirror_horizon_sec():
    base = TrajectoryBaseline()
    assert base.horizon_sec == 5.0
    meta = MetaPredictor(base=base, bank=ResidualBank())
    assert meta.horizon_sec == 5.0


def test_meta_confidence_drops_when_bucket_warming_up():
    """Bucket with fewer than 5 samples → certainty clamped 0.6."""
    base = TrajectoryBaseline()
    bank = ResidualBank()
    features = {"cpu_temp_max": 60.0, "cpu_temp_slope_per_sec": 0.0}
    bank.observe(features, residual_c=-5.0)
    bank.observe(features, residual_c=-5.0)  # only 2 samples
    meta = MetaPredictor(base=base, bank=bank)
    base_pred = base.predict(features, [])
    meta_pred = meta.predict(features, [])
    # base confidence 0.5; meta multiplies in 0.6 → composed < 0.5
    assert meta_pred.confidence < base_pred.confidence


def test_meta_clamps_to_silicon_envelope():
    """Even if base + correction would exit 20-120°C, output is clamped."""
    base = TrajectoryBaseline()
    bank = ResidualBank()
    features = {"cpu_temp_max": 115.0, "cpu_temp_slope_per_sec": 3.0}
    # Observe a large positive correction
    for _ in range(30):
        bank.observe(features, residual_c=+25.0)
    meta = MetaPredictor(base=base, bank=bank)
    pred = meta.predict(features, [])
    assert pred.expected_temp_c <= 120.0


def test_meta_reason_carries_diagnostic_fields():
    base = TrajectoryBaseline()
    bank = ResidualBank()
    features = {"cpu_temp_max": 60.0, "cpu_temp_slope_per_sec": 0.0}
    for _ in range(10):
        bank.observe(features, residual_c=-3.0)
    meta = MetaPredictor(base=base, bank=bank)
    pred = meta.predict(features, [])
    assert "meta-correction" in pred.reason
    assert "bucket" in pred.reason
    assert "n=" in pred.reason


def test_meta_bucket_helper():
    base = TrajectoryBaseline()
    meta = MetaPredictor(base=base, bank=ResidualBank())
    bucket = meta.bucket({
        "cpu_load_slope_per_sec": -2.0,
        "cpu_temp_slope_per_sec": -0.1,
        "cpu_temp_accel_per_sec_sq": -0.05,
        "cpu_temp_max": 65.0,
        "cpu_load_max": 45.0,  # mid load band (v2 axis added 2026-05-13)
    })
    # v3 (2026-05-13) added a 6th axis for thermal-history phase; no
    # temp_now/avg_5min here → plateau (1) is the safe default.
    assert bucket == (-1, -1, -1, 1, 1, 1)
