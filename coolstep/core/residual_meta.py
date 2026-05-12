"""Residual meta-predictor — the "predictor of predictor" layer.

Architecture (per the user's request: "math analysis, geometry, statistics,
compound interest, logarithms as wrapper" — stdlib only, no scipy/ML
black boxes):

  L1   base forecast (TrajectoryBaseline) — Newton-cooling saturation
  L2   residual learner (this module, ResidualBank) — learns systematic
       bias per (load_band, slope_sign, accel_sign, profile_band) bucket
  L3   drift watcher (this module, Cusum) — flags when L2's own learning
       has drifted (regime change), triggers a soft decay of L2's state

Math choices, justified in line:

  Welford's algorithm for running variance       — stdlib statistics
                                                   doesn't expose it
                                                   incrementally
  Log-space residuals: sign(r)·log1p(|r|)        — stabilises variance
                                                   across thermal regimes
                                                   (a 20°C outlier
                                                   doesn't dominate the
                                                   EWMA forever)
  EWMA "compound" weighting                       — newer observations
                                                   have higher weight,
                                                   exponentially.  Same
                                                   shape as Newton
                                                   cooling, just on a
                                                   different signal.
  CUSUM (cumulative sum) for change detection     — two-sided drift
                                                   detector, single
                                                   pass, no windowing.
  Log-odds confidence composition                 — additive on the
                                                   log-odds scale,
                                                   Bayes-friendly.

See ADR-017 in docs/stack-decisions.md.

Public surface:

    quantise_load_band(slope, span) -> int                    # {-1, 0, +1}
    quantise_slope_sign(slope) -> int                         # {-1, 0, +1}
    quantise_accel_sign(accel) -> int                         # {-1, 0, +1}
    quantise_profile_band(temp) -> int                        # {0, 1, 2}  cold/warm/hot
    bucket_of(features) -> tuple[int, int, int, int]
    BucketKey = tuple[int, int, int, int]
    RunningStat                          # Welford incremental stats
    ResidualBank                         # bucket → RunningStat dict
    Cusum                                # two-sided change detector
    LogOddsConfidence                    # composition helper
    log_residual(r) / inv_log_residual   # stabilising transform
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterator

from coolstep.core.residual_log import ResidualLog


BucketKey = tuple[int, int, int, int]   # (load_band, slope_sign, accel_sign, profile_band)


# --- quantisation helpers ----------------------------------------------------

def quantise_load_band(load_slope_per_sec: float | None) -> int:
    """Sign of load trajectory.  -1 falling, 0 flat, +1 rising.

    Threshold ±0.5%/s: below that we call it "flat" (sensor jitter on
    light workloads is easily ±0.3%/s)."""
    if load_slope_per_sec is None or not math.isfinite(load_slope_per_sec):
        return 0
    if load_slope_per_sec > 0.5:
        return 1
    if load_slope_per_sec < -0.5:
        return -1
    return 0


def quantise_slope_sign(slope_per_sec: float | None) -> int:
    """Sign of temperature slope.  Threshold ±0.05°C/s — below that the
    chip is essentially in steady state, sensor jitter dominates."""
    if slope_per_sec is None or not math.isfinite(slope_per_sec):
        return 0
    if slope_per_sec > 0.05:
        return 1
    if slope_per_sec < -0.05:
        return -1
    return 0


def quantise_accel_sign(accel_per_sec_sq: float | None) -> int:
    """Sign of temperature acceleration (d²T/dt²).  Threshold ±0.02°C/s²
    — below that the slope is essentially linear.

    Physical reading: positive accel = ramp accelerating (heat soak
    hasn't reached steady state), negative accel = ramp decelerating
    (approaching equilibrium OR active cooling started)."""
    if accel_per_sec_sq is None or not math.isfinite(accel_per_sec_sq):
        return 0
    if accel_per_sec_sq > 0.02:
        return 1
    if accel_per_sec_sq < -0.02:
        return -1
    return 0


def quantise_profile_band(cpu_temp_c: float | None) -> int:
    """Three thermal bands by current chip temperature.  0 cold (<60°C),
    1 warm (60-80°C), 2 hot (≥80°C).  Same bias appears in different
    bands at very different scales; stratifying captures that."""
    if cpu_temp_c is None or not math.isfinite(cpu_temp_c):
        return 1
    if cpu_temp_c < 60.0:
        return 0
    if cpu_temp_c < 80.0:
        return 1
    return 2


def bucket_of(features: dict[str, float]) -> BucketKey:
    """Project a features dict into a discrete bucket coordinate.

    Inputs read (all may be missing — defaults are safe):
      cpu_load_slope_per_sec  → load_band (-1/0/+1)
      cpu_temp_slope_per_sec  → slope_sign (-1/0/+1)
      cpu_temp_accel_per_sec_sq → accel_sign (-1/0/+1)
      cpu_temp_max            → profile_band (0/1/2)
    """
    return (
        quantise_load_band(features.get("cpu_load_slope_per_sec")),
        quantise_slope_sign(features.get("cpu_temp_slope_per_sec")),
        quantise_accel_sign(features.get("cpu_temp_accel_per_sec_sq")),
        quantise_profile_band(features.get("cpu_temp_max")),
    )


# --- log-space residual transform -------------------------------------------

def log_residual(r: float) -> float:
    """Sign-preserving log compression: sign(r)·log1p(|r|).

    Why: residuals can occasionally hit ±20°C on regime change.  In
    linear space, one such outlier dominates an EWMA mean (e.g.
    α=0.05 still gives the outlier 5% of forever).  In log space,
    log1p(20)=3.04 vs log1p(2)=1.10 — outlier is 2.8× influential
    instead of 10× influential.  Convergence to the true bias is
    cleaner under realistic outlier patterns.

    Linear at small |r| (log1p(0.5) ≈ 0.405, vs 0.5 itself) — small
    residuals are nearly untouched.  Heavy at large |r|."""
    if not math.isfinite(r):
        return 0.0
    return math.copysign(math.log1p(abs(r)), r)


def inv_log_residual(lr: float) -> float:
    """Inverse of log_residual: sign(lr)·(exp(|lr|)-1)."""
    if not math.isfinite(lr):
        return 0.0
    return math.copysign(math.expm1(abs(lr)), lr)


# --- running statistics ------------------------------------------------------

@dataclass
class RunningStat:
    """Welford's online algorithm for running mean and variance, with an
    EWMA "compound" weighting overlay.

    Two views over the same data stream:

      mean / var / std   classical running estimate (Welford)
      ewma_mean          exponentially-weighted, alpha-controlled

    The classical view is unbiased and total-sample.  The EWMA view is
    recency-biased and reacts faster to regime shifts.  We expose both;
    consumers pick what they need.

    decay(factor) shrinks all accumulator state — used on profile flip
    to acknowledge "prior learning is partially stale, but not garbage".
    """

    n: float = 0.0       # float so decay(0.5) doesn't lose to int truncation
    mean: float = 0.0
    m2: float = 0.0
    ewma_mean: float = 0.0
    alpha: float = 0.05   # ~last 20 observations dominate the EWMA

    def observe(self, x: float) -> None:
        if not math.isfinite(x):
            return
        self.n += 1.0
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.m2 += delta * delta2
        # EWMA: warm up linearly until alpha "saturates"
        if self.n == 1:
            self.ewma_mean = x
        else:
            self.ewma_mean = (1 - self.alpha) * self.ewma_mean + self.alpha * x

    def variance(self) -> float:
        if self.n < 2:
            return 0.0
        return self.m2 / (self.n - 1)

    def std(self) -> float:
        return math.sqrt(self.variance())

    def decay(self, factor: float) -> None:
        """Multiplicatively shrink the sample state.  factor ∈ [0, 1].
        Keeps means + alpha unchanged (mean is the "where", n is the
        "how confident").  factor=0.5 ≈ "I've seen half as many samples
        as I thought" — fast re-convergence."""
        if not 0.0 <= factor <= 1.0:
            return
        self.n *= factor
        self.m2 *= factor

    def reset(self) -> None:
        self.n = 0.0
        self.mean = 0.0
        self.m2 = 0.0
        self.ewma_mean = 0.0


# --- residual bank ----------------------------------------------------------

@dataclass
class ResidualBank:
    """`dict[BucketKey, RunningStat]` with log-space residual storage
    and convenience accessors.

    Workflow:

      bank.observe(features, residual_c)        # at validation moment
      mean, std = bank.correct(features)        # at next prediction
      predicted_corrected = base.expected + mean

    The bank stores residuals in *log space* (see `log_residual`).
    `correct()` returns the inverse-transformed mean — i.e. a number you
    can add to a temperature directly.

    `decay_all(factor)` is the soft-reset path used on profile flip or
    CUSUM trip.
    """

    stats: dict[BucketKey, RunningStat] = field(default_factory=dict)
    alpha: float = 0.05

    def observe(self, features: dict[str, float], residual_c: float) -> BucketKey:
        """Record a residual at the bucket implied by the prediction-time
        features.  Returns the bucket key for the caller's reference."""
        key = bucket_of(features)
        stat = self.stats.get(key)
        if stat is None:
            stat = RunningStat(alpha=self.alpha)
            self.stats[key] = stat
        stat.observe(log_residual(residual_c))
        return key

    def observe_at_bucket(self, key: BucketKey, residual_c: float) -> None:
        """Direct bucket-keyed observe — used by `from_log` to rebuild
        state from records that already carry their bucket_key."""
        stat = self.stats.get(key)
        if stat is None:
            stat = RunningStat(alpha=self.alpha)
            self.stats[key] = stat
        stat.observe(log_residual(residual_c))

    def correct(self, features: dict[str, float]) -> tuple[float, float, int]:
        """Return (correction_c, std_c, sample_count).

        correction_c: additive correction to add to the base forecast.
                      0.0 when no observations exist for this bucket.
        std_c: standard deviation of *linear* residuals for this bucket,
               used by the dashboard as ±σ uncertainty band.
        sample_count: integer round of stat.n (post-decay it's a float)."""
        key = bucket_of(features)
        stat = self.stats.get(key)
        if stat is None or stat.n < 1:
            return 0.0, 0.0, 0
        correction = inv_log_residual(stat.ewma_mean)
        # std lives in log-space too; convert by mapping ±std around 0
        # back into linear residual space.
        std_linear = max(
            abs(inv_log_residual(stat.std())),
            0.0,
        )
        return correction, std_linear, int(stat.n)

    def decay_all(self, factor: float) -> None:
        for stat in self.stats.values():
            stat.decay(factor)

    def items(self) -> Iterator[tuple[BucketKey, RunningStat]]:
        return iter(self.stats.items())

    def __len__(self) -> int:
        return len(self.stats)

    @classmethod
    def from_log(cls, log: ResidualLog, *, alpha: float = 0.05) -> ResidualBank:
        """Rebuild bank state by replaying every record in the log.
        Used at daemon startup so a restart doesn't lose learned bias.

        Records without a `bucket_key` (Phase 1 entries written before
        the meta-predictor was wired) are silently skipped — their
        features dict is still available for *future* bucket assignment
        but rebuilding implicit-bucket-from-features on every startup is
        wasted I/O when the dashboard's residual-trail view doesn't care."""
        bank = cls(alpha=alpha)
        for rec in log.iter_all():
            if rec.bucket_key is not None and len(rec.bucket_key) == 4:
                bank.observe_at_bucket(tuple(rec.bucket_key), rec.residual_c)  # type: ignore[arg-type]
        return bank


# --- CUSUM drift detector ----------------------------------------------------

@dataclass
class Cusum:
    """Two-sided CUSUM (cumulative sum) for detecting mean-shift in the
    residual stream.

    Math:  s_up   = max(0, s_up   + (x - mu - k))
           s_down = max(0, s_down + (-x + mu - k))
    Trip when either crosses `h`.  `k` and `h` are in *log-residual*
    units (matches the substrate the bank stores).

    Tuned defaults: k ≈ 0.5·σ, h ≈ 4·σ for σ ≈ 1.0 in log-space
    (corresponds to ~e°C linear residual std, generous).

    On trip, the consumer is expected to:
      1. call ResidualBank.decay_all(0.5)
      2. reset this CUSUM
      3. log the event
    """

    mu: float = 0.0
    k: float = 0.5
    h: float = 4.0
    s_up: float = 0.0
    s_down: float = 0.0

    def feed(self, x: float) -> str | None:
        """Return "up" / "down" if this observation tripped the threshold,
        else None.  After a trip the corresponding accumulator is auto-
        reset so the next trip needs another full-crossing."""
        if not math.isfinite(x):
            return None
        self.s_up = max(0.0, self.s_up + (x - self.mu - self.k))
        self.s_down = max(0.0, self.s_down + (-x + self.mu - self.k))
        if self.s_up > self.h:
            self.s_up = 0.0
            return "up"
        if self.s_down > self.h:
            self.s_down = 0.0
            return "down"
        return None

    def reset(self) -> None:
        self.s_up = 0.0
        self.s_down = 0.0


# --- log-odds confidence composition ----------------------------------------

def confidence_to_log_odds(p: float) -> float:
    """Map confidence p ∈ (0, 1) to log-odds  log(p / (1-p)).
    Clamps near 0/1 to avoid ±inf."""
    p = max(1e-6, min(1.0 - 1e-6, p))
    return math.log(p / (1.0 - p))


def log_odds_to_confidence(lo: float) -> float:
    """Inverse: confidence = 1 / (1 + exp(-lo))."""
    if lo >= 0:
        return 1.0 / (1.0 + math.exp(-lo))
    e = math.exp(lo)
    return e / (1.0 + e)


def compose_confidence(*confidences: float) -> float:
    """Combine multiple confidence sources additively on the log-odds
    scale.  Two sources at 0.7 each compose to ~0.84 — agreement
    increases confidence; disagreement (one high, one low) cancels.
    Mathematically equivalent to multiplying odds."""
    if not confidences:
        return 0.5
    total = sum(confidence_to_log_odds(c) for c in confidences)
    return log_odds_to_confidence(total)


__all__ = [
    "BucketKey",
    "quantise_load_band",
    "quantise_slope_sign",
    "quantise_accel_sign",
    "quantise_profile_band",
    "bucket_of",
    "log_residual",
    "inv_log_residual",
    "RunningStat",
    "ResidualBank",
    "Cusum",
    "confidence_to_log_odds",
    "log_odds_to_confidence",
    "compose_confidence",
]
