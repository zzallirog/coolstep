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
    quantise_load_pct_band(load_max) -> int                   # {0, 1, 2}  idle/mid/high  (v2 2026-05-13)
    quantise_temp_phase(temp_now, temp_avg_5min) -> int       # {0, 1, 2}  asc/plateau/desc (v3 2026-05-13)
    bucket_of(features) -> tuple[int, int, int, int, int, int]
    BucketKey = tuple[int, int, int, int, int, int]
    RunningStat                          # Welford incremental stats
    ResidualBank                         # bucket → RunningStat dict
    Cusum                                # two-sided change detector
    LogOddsConfidence                    # composition helper
    log_residual(r) / inv_log_residual   # stabilising transform
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from coolstep.core.residual_log import ResidualLog

BucketKey = tuple[int, int, int, int, int, int]
# (load_slope_band, temp_slope_sign, accel_sign, temp_profile_band,
#  load_pct_band, temp_history_phase)


# --- trust-mode classifier --------------------------------------------------

class TrustMode(StrEnum):
    """Bayesian shrinkage regime for a given sample count.

    PRIOR     — n == 0: no data, returns prior (0 correction, σ₀ band).
    SHRUNK    — 0 < n < PRIOR_K: correction damped ~60%, σ wide.
    CONFIDENT — n >= PRIOR_K: approaches pure-EWMA estimate, σ narrow.
    """

    PRIOR     = "prior"
    SHRUNK    = "shrunk"
    CONFIDENT = "confident"


def classify_trust(n: int) -> TrustMode:
    """Return the trust regime for a bucket with *n* samples."""
    if n == 0:
        return TrustMode.PRIOR
    if n < int(PRIOR_K):
        return TrustMode.SHRUNK
    return TrustMode.CONFIDENT
# v2 (2026-05-13): added load_pct_band as 5th axis. Previously bucket (0,0,0,1)
# pooled idle-warm AND active-warm workloads, so a per-bucket EWMA bias learned
# under one regime was applied to the other (operator-flagged: a background ML
# workload kicking in from a reduced-resource schedule produced systematic
# +20°C residuals because the bucket was holding -10°C correction learned
# during a different workload). Splitting by current load_max bands
# (idle/mid/high) prevents that pooling.
#
# v3 (2026-05-13): added temp_history_phase as 6th axis. Operator-flagged
# pathology: bucket (0,-1,1,1,0) in live cockpit carried mean_residual=-9°C
# with σ_linear=6.78 across n=67 — clearly pooling two regimes. Same five
# axis-values appear both on ramp-up (chip arriving at 70°C from below) and
# on wind-down (chip leaving 70°C from above); future peak_temp differs by
# 10°C+ between those phases, so the bucket learns the average of two
# opposite-sign systematic biases. Phase axis derived from T_now − T_avg_5min
# splits the pool: each sub-bucket gets a clean bias (~+2°C ramp-up,
# ~−3°C wind-down) and σ collapses naturally — no need for σ-shrinkage
# (variant 2, shipped+reverted) or rule-based sanity gates (variant 3,
# rejected by operator). KNN's killer signal "я был тут, через 30s стало
# плохо" untouched — only the bookkeeping layer becomes regime-aware.


# --- Bayesian shrinkage prior ------------------------------------------------
#
# Why this exists:  the cockpit live-verified an overconfidence pathology
# (image kindly attached by the operator, 2026-05-12).  With n=2 samples
# in a fresh bucket, Welford's variance was 0.01°C² (both samples agreed
# to within noise), giving a σ ≈ 0.1°C-band on a correction of −6.23°C.
# That's a strong claim from two observations.  The dashboard then drew
# a confident +5s forecast that overstated certainty by ~10×.
#
# Fix: treat each bucket as starting with k pseudo-observations at zero
# mean and PRIOR_LINEAR_C linear std.  Mean and variance shrink toward
# this prior at small n, and recover the pure-data view as n grows.
# Math: standard normal-inverse-gamma posterior, no scipy needed:
#   μ_post   = (n·m_data + k·0)               / (n + k)
#   ν_post²  = (m2 + k·σ₀² + n·k/(n+k)·m²)    / (n + k - 1)
# The third term in ν_post² is the data-vs-prior disagreement variance —
# wider σ when data and prior disagree, which is exactly the honesty we
# want at small n with a strong data signal.
#
# k=5 chosen so the cockpit's first ~3 observations of a new bucket are
# treated as preliminary (shrinkage damps the correction by ~60%) and
# convergence to pure data happens by ~15 observations.  PRIOR_LINEAR_C
# = 2°C: residual scale we expect on a tuned predictor in calibrated
# regime.
PRIOR_K: float = 5.0
PRIOR_LINEAR_C: float = 2.0
PRIOR_LOG_SIGMA: float = math.log1p(PRIOR_LINEAR_C)


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


def quantise_load_pct_band(load_pct: float | None) -> int:
    """Three CPU-load bands by current max-core load.  0 idle (<30%),
    1 mid (30-70%), 2 high (≥70%).  Added 2026-05-13 as bucket-of v2's
    5th axis: idle and active workloads share thermal trajectory shape
    (slope/accel/temp) but produce systematically different residual
    bias, so pooling them under the same bucket leaks correction across
    regimes."""
    if load_pct is None or not math.isfinite(load_pct):
        return 1
    if load_pct < 30.0:
        return 0
    if load_pct < 70.0:
        return 1
    return 2


def quantise_temp_phase(
    cpu_temp_now: float | None, cpu_temp_avg_5min: float | None
) -> int:
    """Three thermal-history phases derived from T_now − T_avg_5min.

    0 ascending  — Δ ≥ +3°C; chip rode up recently, ramp-up regime
    1 plateau    — |Δ| < 3°C; steady-state / direction not clear
    2 descending — Δ ≤ −3°C; chip cooled recently, wind-down regime

    Threshold ±3°C chosen so 5-min average jitter (idle-load
    micro-fluctuations on Ryzen 7940HS ≈ ±1.5°C around equilibrium)
    doesn't trip a phase flip. Same instantaneous (slope, accel) signal
    appears in both ramp-up and wind-down — phase disambiguates them
    using *recent history* without requiring the predictor to see the
    full trajectory.

    Missing inputs (cold start, <5 min of ring data) → 1 (plateau),
    which is the natural "no information" answer: a bucket entered as
    plateau decays toward its true bias over a few minutes once
    cpu_temp_avg_5min stabilises."""
    if cpu_temp_now is None or not math.isfinite(cpu_temp_now):
        return 1
    if cpu_temp_avg_5min is None or not math.isfinite(cpu_temp_avg_5min):
        return 1
    delta = cpu_temp_now - cpu_temp_avg_5min
    if delta >= 3.0:
        return 0
    if delta <= -3.0:
        return 2
    return 1


def bucket_of(features: dict[str, float]) -> BucketKey:
    """Project a features dict into a discrete bucket coordinate.

    Inputs read (all may be missing — defaults are safe):
      cpu_load_slope_per_sec    → load_slope_band (-1/0/+1)
      cpu_temp_slope_per_sec    → temp_slope_sign (-1/0/+1)
      cpu_temp_accel_per_sec_sq → accel_sign (-1/0/+1)
      cpu_temp_max              → temp_profile_band (0/1/2)
      cpu_load_max              → load_pct_band (0/1/2)   (v2 2026-05-13)
      cpu_temp_now,             → temp_history_phase (0/1/2)
      cpu_temp_avg_5min                                   (v3 2026-05-13)
    """
    return (
        quantise_load_band(features.get("cpu_load_slope_per_sec")),
        quantise_slope_sign(features.get("cpu_temp_slope_per_sec")),
        quantise_accel_sign(features.get("cpu_temp_accel_per_sec_sq")),
        quantise_profile_band(features.get("cpu_temp_max")),
        quantise_load_pct_band(features.get("cpu_load_max")),
        quantise_temp_phase(
            features.get("cpu_temp_now"),
            features.get("cpu_temp_avg_5min"),
        ),
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
        """Return (correction_c, std_c, sample_count) with Bayesian
        shrinkage toward a zero-mean PRIOR_LINEAR_C-σ prior.

        correction_c: additive correction to add to the base forecast.
                      Shrunk by n/(n+k): at n=0 it's exactly 0; at
                      n=k it's halved; as n→∞ it approaches the pure
                      EWMA correction.
        std_c: posterior std in linear-residual space.  Combines the
               bucket's accumulated m2 with k·σ₀² pseudo-variance and a
               data-vs-prior disagreement term — so a confident-looking
               bucket with only 2 agreeing samples reports a σ band the
               operator can read as "trust this only modestly".
        sample_count: integer round of stat.n (post-decay it's a float).
        """
        key = bucket_of(features)
        stat = self.stats.get(key)
        if stat is None or stat.n < 1:
            # No data yet — return the prior view: 0 correction with
            # the prior σ-band so the dashboard renders a "we don't
            # know yet" band instead of a zero-width one.
            prior_sigma_linear = abs(inv_log_residual(PRIOR_LOG_SIGMA))
            return 0.0, prior_sigma_linear, 0
        n = float(stat.n)
        n_eff = n + PRIOR_K
        # Mean shrinkage: posterior mean of a normal with conjugate prior
        # (μ₀=0, k pseudo-obs).  At small n this damps the correction
        # toward zero; at large n it relaxes to the EWMA estimate.
        mean_log_shrunk = stat.ewma_mean * n / n_eff
        # Variance shrinkage with disagreement term.  Posterior m2:
        #   m2_post = m2_data + k·σ₀² + (n·k/n_eff)·(m_data − μ₀)²
        # The third term grows whenever the data mean disagrees with the
        # prior mean — that's the σ widening that prevents over-
        # confidence on a confident-but-young bucket.
        disagreement = (n * PRIOR_K / n_eff) * stat.ewma_mean ** 2
        m2_combined = stat.m2 + PRIOR_K * PRIOR_LOG_SIGMA ** 2 + disagreement
        var_log_shrunk = m2_combined / max(1.0, n_eff - 1.0)
        std_log_shrunk = math.sqrt(var_log_shrunk)
        correction = inv_log_residual(mean_log_shrunk)
        std_linear = abs(inv_log_residual(std_log_shrunk))
        return correction, std_linear, int(stat.n)

    def correct_with_trust(
        self, features: dict[str, float]
    ) -> tuple[float, float, int, TrustMode]:
        """Return (correction_c, std_c, sample_count, TrustMode).

        Same math as `correct()` — adds the trust-mode classification so
        callers (daemon state, dashboard) can show which shrinkage regime
        is active without reimplementing the threshold logic.
        """
        correction, std, n = self.correct(features)
        return correction, std, n, classify_trust(n)

    def decay_all(self, factor: float) -> None:
        for stat in self.stats.values():
            stat.decay(factor)

    def items(self) -> Iterator[tuple[BucketKey, RunningStat]]:
        return iter(self.stats.items())

    def __len__(self) -> int:
        return len(self.stats)

    @classmethod
    def from_log(
        cls,
        log: ResidualLog,
        *,
        alpha: float = 0.05,
        include_intervened: bool = False,
    ) -> ResidualBank:
        """Rebuild bank state by replaying every record in the log.
        Used at daemon startup so a restart doesn't lose learned bias.

        Migration v3 (2026-05-13): bucket axis count grew 4 → 5 → 6 over
        two same-day patches (load_pct_band, then temp_history_phase). On
        disk we may see any of the three shapes; the current `BucketKey`
        is 6-tuple — applying an older shape as-is would crash dict
        access in `correct()` (mixed key shapes).

        Re-bucket from `features` if present: this both upgrades old
        records to the new schema AND lets a future axis change land
        without re-writing the log. Records with neither a usable
        features dict nor a 6-tuple bucket_key are skipped.

        - 6-tuple bucket_key (current schema) → use as-is
        - 4/5-tuple + features → re-bucket via bucket_of(features)
        - non-6-tuple, no features (very old) → skip

        Phase axis on re-bucketed v1/v2 records: features without
        cpu_temp_now / cpu_temp_avg_5min land in plateau (1) — the
        intended cold-start regime, decays to its true sub-phase once
        live observations arrive.

        Controlled residuals (``intervened=True``) stay in the log for
        cockpit/post-mortem use but are skipped by default. The bank is a
        passive correction layer; training it on samples whose horizon was
        changed by a fan/power actuator would fold coolstep's own action
        back into the model as if it were natural thermal drift."""
        bank = cls(alpha=alpha)
        for rec in log.iter_all():
            if rec.intervened and not include_intervened:
                continue
            key: BucketKey | None = None
            if rec.bucket_key is not None and len(rec.bucket_key) == 6:
                key = tuple(rec.bucket_key)  # type: ignore[assignment]
            elif rec.features:
                key = bucket_of(rec.features)
            if key is not None:
                bank.observe_at_bucket(key, rec.residual_c)
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
    "TrustMode",
    "classify_trust",
    "quantise_load_band",
    "quantise_slope_sign",
    "quantise_accel_sign",
    "quantise_profile_band",
    "quantise_load_pct_band",
    "quantise_temp_phase",
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
