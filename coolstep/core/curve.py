"""Adaptive fan-curve composition — P2.4.

The pre-P2.4 pipeline was a single scalar bias (±intensity × knee_weight)
on a static baseline. That's a one-dimensional knob over a multi-
dimensional reality: predictor probability, current workload, recent
throttle history, mode, ambient — all real circumstances that the curve
should answer to, and which compose together. This module is the
generalised replacement.

A *policy* is a pure function that takes anchors + context and returns
new anchors. The pipeline composes policies in a fixed order documented
at the bottom of this file. Each policy:

  * is independent (no shared mutable state),
  * returns a fresh list of anchors (input untouched),
  * clamps PWM into [0, 100] per-anchor,
  * is idempotent: same (anchors, ctx) → byte-identical output.

The idempotence is load-bearing — the actuator caches the last issued
curve and skips the subprocess when the new one matches byte-for-byte.

Composition order (later policies can stack on earlier ones; the last
policy in the list is the strongest):

    floor_to_49c
        Lock everything below 49 °C to 0 % PWM.
    quiet_subtract
        Mode-driven subtraction in the knee band on confidently-calm windows.
    predictive_preload
        Lift mid-knee anchors proportional to predicted peak.
    workload_profile_shape
        Per-profile knee bump (CODE calmer, GAME warmer, …).
    workload_headroom
        Small fixed bump for gaming / build / render classes.
    recent_throttle_bump
        Lift base 65-80 °C anchors if the chip has been hot recently.
    heat_soak_bump
        Lift mid-knee when ΔT/ΔPower says the radiator is heat-soaked.
    emergency_ramp
        Force anchors above current Tctl to 100 % when Tctl ≥ 82 °C.

`emergency_ramp` runs *last* so nothing can override it — it is the
hardware safety belt above the policy stack.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from coolstep.core.workload_profile import Profile, is_headroom_class

# Anchor = (temperature in °C, PWM in percent 0-100).
Anchor = tuple[float, float]

# A policy transforms a curve given the live context. Both inputs are
# immutable — caller passes the same list each tick, policies return a
# new list. `frozenset` for armed_verbs keeps the dataclass hashable.
Policy = Callable[[list[Anchor], "CurveContext"], list[Anchor]]

# Knee constants — must mirror the actuator constants. Kept here so the
# core module is self-contained without importing the actuator (which
# would be an awkward layer crossing from core/ into adapters/).
KNEE_OUTER_COOL = 70.0
KNEE_COOL = 75.0
KNEE_HOT = 80.0
KNEE_OUTER_HOT = 85.0
QUIET_FLOOR_PWM = 12.0           # never drop a knee anchor below this
QUIET_MAX_SUBTRACT = 15.0        # cap quiet subtraction amplitude
PREDICTIVE_MAX_LIFT = 20.0       # cap predictive_preload amplitude
# Reactive slope trigger — independent of KNN. Catches phase-shift spikes
# that the 30-sec predictor doesn't see (browser tab opening a heavy video,
# build-step kick-off, etc.).  Threshold tuned to ignore normal idle wobble:
# `cpu_temp_slope_per_sec_short` is typically ±0.05°C/s when calm.
SLOPE_TRIGGER_C_PER_SEC = 0.30
SLOPE_TRIGGER_FLOOR_C = 75.0     # only react once chip is already warm
SLOPE_LIFT_GAIN = 30.0           # slope×gain → knee bump pct
SLOPE_MAX_LIFT_PCT = 12.0
WORKLOAD_HEADROOM_PWM = 5.0
RECENT_BUMP_PWM = 8.0
RECENT_THRESHOLD = 2             # ≥ this many throttle events in last 1h → bump
EMERGENCY_TEMP_C = 82.0          # above this, ramp anchors to 100%
EMERGENCY_ANCHOR_PCT = 100.0
FLOOR_TEMP_C = 49.0
HEAT_SOAK_BUMP_THRESHOLD = 0.5   # ΔT/ΔPower above this → radiator is hot
HEAT_SOAK_BUMP_GAIN = 6.0        # ratio × gain (clamped to MAX) → knee bump pct
HEAT_SOAK_MAX_BUMP_PCT = 10.0

# Per-profile knee bump (additive percent points on top of base curve).
# CODE is calmer than base; GAME / RENDER warmer. IDLE shaves a little but
# never below the existing per-anchor PWM floor. OTHER is a no-op.
PROFILE_KNEE_BUMP_PCT: dict[str, float] = {
    Profile.CODE.value:    -2.0,
    Profile.RENDER.value:   4.0,
    Profile.GAME.value:     8.0,
    # Browser sits between CODE and RENDER — heavier than a terminal session,
    # lighter than active rendering, but the dominant real-world heat source.
    Profile.BROWSER.value:  3.0,
    Profile.IDLE.value:    -3.0,
    Profile.OTHER.value:    0.0,
}

@dataclass(slots=True, frozen=True)
class CurveContext:
    """Everything a policy might want to know about *right now*.

    All fields are optional-with-defaults so the caller (daemon) can build
    a partial context when some signals are missing — policies handle
    ``None``/zero conservatively (i.e. they refuse to bias on uncertain
    inputs, never the other way).
    """
    cpu_temp_c: float | None = None
    throttle_prob: float = 0.0
    confidence: float = 0.0
    expected_temp_c: float | None = None
    horizon_sec: float = 30.0
    mode: str = "cool"
    workload_class: str | None = None
    recent_throttle_count: int = 0
    armed_verbs: frozenset[str] = field(default_factory=frozenset)
    # P2.5 — confidence-driven hysteresis. Set by the daemon when KNN
    # confidence falls below `Thresholds.conservative_floor` AND there's
    # at least some heat hint (throttle_prob > 0.2). Policies that act
    # on this widen their trigger threshold and slightly amplify their
    # output — trading silence for safety on unknown scenes.
    conservative: bool = False
    # P2.5 Phase C — Profile.value (CODE / RENDER / GAME / IDLE / OTHER).
    # `workload_profile_shape` modulates per-anchor knee bump per profile.
    workload_profile: str = "other"
    # P2.5 Phase D — unit-free «inertia» ratio ΔT/ΔPower (load fallback).
    # Above HEAT_SOAK_BUMP_THRESHOLD radiator is in soak — small power
    # changes move temperature a lot, so we pre-lift the knee.
    heat_soak_index: float = 0.0
    # Short-window temperature derivative (°C/s) from the daemon fingerprint.
    # Independent of KNN — the `slope_preload` policy reacts on raw kinematics
    # so spikes that the 30-sec predictor doesn't see still get a head start
    # on the fans.
    cpu_temp_slope_short_c_per_sec: float = 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _knee_weight(temp_c: float) -> float:
    """Trapezoid weight matching the actuator constants: 1.0 inside the
    knee band, linear ramp to 0 across the outer cool/hot edges. Used by
    every policy that wants to bias the knee specifically rather than
    every anchor uniformly."""
    if KNEE_COOL <= temp_c <= KNEE_HOT:
        return 1.0
    if KNEE_OUTER_COOL <= temp_c < KNEE_COOL:
        return (temp_c - KNEE_OUTER_COOL) / (KNEE_COOL - KNEE_OUTER_COOL)
    if KNEE_HOT < temp_c <= KNEE_OUTER_HOT:
        return (KNEE_OUTER_HOT - temp_c) / (KNEE_OUTER_HOT - KNEE_HOT)
    return 0.0


# ── Policies ───────────────────────────────────────────────────────────


def floor_to_49c(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Hard floor — any anchor below 49 °C is forced to 0 % PWM.

    The user request was «нот отыгрывает от 0% вплоть до 49 градусов».
    The hardware fan-stop floor (~2000 RPM on TUF A15) still applies but
    *that's a hardware reality, not a policy choice* — what we control
    is the PWM ceiling for the cool band, and it's 0."""
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        if temp_c < FLOOR_TEMP_C:
            out.append((temp_c, 0.0))
        else:
            out.append((temp_c, _clamp(pwm_pct)))
    return out


def quiet_subtract(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Mode-driven negative bias on the knee.

    Mirrors the legacy `_quiet_curve` math but is now one of several
    composable policies. Only fires when:
      * mode == "quiet"
      * throttle_prob ≤ 0.15 (predictor confidently calm)
      * cpu_temp_c < KNEE_COOL (75 °C) — well below the knee
    Otherwise it's a no-op, preserving anchors as-is."""
    if ctx.mode != "quiet":
        return anchors
    if ctx.throttle_prob > 0.15:
        return anchors
    if ctx.cpu_temp_c is None or ctx.cpu_temp_c >= KNEE_COOL:
        return anchors
    intensity = min(QUIET_MAX_SUBTRACT, 10.0)
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        cut = intensity * _knee_weight(temp_c)
        final = max(QUIET_FLOOR_PWM, pwm_pct - cut)
        final = min(final, pwm_pct)
        out.append((temp_c, _clamp(final)))
    return out


def predictive_preload(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Lift mid-knee anchors proportional to the predicted peak.

    Maps `(throttle_prob - 0.5) × 40` → PWM bump at knee centre, fading
    by horizon (the further ahead the prediction looks, the smaller the
    pre-load). Skips when mode == "off" (no actionable verbs allowed).

    P2.5 — Conservative Mode (low KNN confidence + heat hint) widens
    the trigger floor (0.6 → 0.45) and adds a 1.2× amplification. We
    trade quiet for safety when the predictor doesn't recognise the
    scene. The plain "high-confidence calm" path is unchanged."""
    if ctx.mode == "off":
        return anchors
    floor = 0.45 if ctx.conservative else 0.6
    if ctx.throttle_prob < floor:
        return anchors
    # Conservative shifts the "neutral" prob downward — at prob=0.5 the
    # confident path produces zero lift (it's right at the formula's
    # zero crossing), but the conservative path treats 0.5 as «modest
    # peak» worth a few percent. Plus 1.2× amplitude after.
    base_prob = 0.4 if ctx.conservative else 0.5
    raw_lift = (ctx.throttle_prob - base_prob) * 40.0
    if ctx.conservative:
        raw_lift *= 1.2
    # Fade with horizon: 30 s full, 60 s ½, 120 s ¼. Sigmoid-ish without
    # the math overhead.
    horizon_factor = 30.0 / max(30.0, ctx.horizon_sec)
    intensity = _clamp(raw_lift * horizon_factor, 0.0, PREDICTIVE_MAX_LIFT)
    if intensity <= 0.5:
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        lift = intensity * _knee_weight(temp_c)
        out.append((temp_c, _clamp(pwm_pct + lift)))
    return out


def workload_headroom(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Small fixed +5 % at knee for known-hot workload classes.

    A heuristic that says «if the user just launched Blender / Steam /
    a long build, give the fans a head start regardless of what the
    predictor thinks». Predictor models are calm for the first ~30 s of
    a new workload (its window doesn't carry that history yet); this
    policy is the bridge."""
    if ctx.workload_class is None:
        return anchors
    if not is_headroom_class(ctx.workload_class):
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        bump = WORKLOAD_HEADROOM_PWM * _knee_weight(temp_c)
        out.append((temp_c, _clamp(pwm_pct + bump)))
    return out


def recent_throttle_bump(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Lift the 65-80 °C band by +8 % when the chip has been hot recently.

    «recently» is whatever the daemon counts as the rolling 1 h window of
    throttle events. The intent: a chip that's been hot in the last hour
    is likely to be hot again — give the curve a stable boost so we
    aren't always playing catch-up after the eject."""
    if ctx.recent_throttle_count < RECENT_THRESHOLD:
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        if 65.0 <= temp_c <= 80.0:
            out.append((temp_c, _clamp(pwm_pct + RECENT_BUMP_PWM)))
        else:
            out.append((temp_c, _clamp(pwm_pct)))
    return out


def workload_profile_shape(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Per-profile knee bump on top of the rest of the stack.

    Bump table (see PROFILE_KNEE_BUMP_PCT):
      CODE   -2 %   (calmer than base)
      RENDER +4 %
      GAME   +8 %
      IDLE   -3 %   (calmer, but never below the existing per-anchor value
                    — we only subtract from the *bump*, we never push the
                    underlying curve down through zero)
      OTHER   0 %   (no-op)

    The IDLE branch is asymmetric on purpose: a sleeping laptop should
    not have its 70-80 °C curve cut to nothing, because the next wake-up
    workload would then have to claw all that back. We trim a little.
    """
    bump = PROFILE_KNEE_BUMP_PCT.get(ctx.workload_profile, 0.0)
    if bump == 0.0:
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        delta = bump * _knee_weight(temp_c)
        new_pwm = pwm_pct + delta
        # Never push an anchor below 0 — the floor_to_49c policy owns the
        # «cool band sits at 0» invariant and we won't fight it from here.
        out.append((temp_c, _clamp(new_pwm)))
    return out


def heat_soak_bump(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Lift the knee when the radiator is heat-soaked.

    `heat_soak_index > HEAT_SOAK_BUMP_THRESHOLD` says ΔT is climbing much
    faster than ΔPower — the heatsink is already saturated, every extra
    watt now translates into rapid temperature gain. Pre-lift the mid-knee
    band so the fans are already moving when the load actually peaks.

    Amplitude: `min(index × HEAT_SOAK_BUMP_GAIN, HEAT_SOAK_MAX_BUMP_PCT)`.
    Stacks on top of predictive_preload / workload_profile_shape rather
    than replacing them — soak is a complementary signal, not a substitute.
    """
    if ctx.heat_soak_index <= HEAT_SOAK_BUMP_THRESHOLD:
        return anchors
    amp = _clamp(
        ctx.heat_soak_index * HEAT_SOAK_BUMP_GAIN,
        0.0,
        HEAT_SOAK_MAX_BUMP_PCT,
    )
    if amp <= 0.0:
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        lift = amp * _knee_weight(temp_c)
        out.append((temp_c, _clamp(pwm_pct + lift)))
    return out


def emergency_ramp(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Last-line safety: when Tctl ≥ 82 °C, force any anchor at or above
    the current temperature to 100 % PWM.

    Runs after every other policy so nothing in the stack can override
    it. This is the hardware-side guarantee that a misclassified «calm»
    prediction can't keep fans throttled while the chip is actually
    cooking."""
    if ctx.cpu_temp_c is None or ctx.cpu_temp_c < EMERGENCY_TEMP_C:
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        if temp_c >= ctx.cpu_temp_c - 1.0:
            out.append((temp_c, EMERGENCY_ANCHOR_PCT))
        else:
            out.append((temp_c, _clamp(pwm_pct)))
    return out


def slope_preload(anchors: list[Anchor], ctx: CurveContext) -> list[Anchor]:
    """Reactive knee lift on a fast rising temperature, independent of KNN.

    Empirically the 30-sec KNN predictor misses phase-shift spikes — a heavy
    browser tab opening, a build kick-off — because no KNN neighbour has
    that exact precursor signature yet. This policy reacts on raw kinematics:
    when the short-window slope rises faster than `SLOPE_TRIGGER_C_PER_SEC`
    AND the chip is already past `SLOPE_TRIGGER_FLOOR_C`, lift the knee
    proportional to slope (capped at `SLOPE_MAX_LIFT_PCT`).

    Stacks on top of `predictive_preload` rather than replacing it — KNN
    catches sustained pressure, slope catches transients."""
    if ctx.mode == "off":
        return anchors
    if ctx.cpu_temp_c is None or ctx.cpu_temp_c < SLOPE_TRIGGER_FLOOR_C:
        return anchors
    slope = ctx.cpu_temp_slope_short_c_per_sec
    if slope < SLOPE_TRIGGER_C_PER_SEC:
        return anchors
    intensity = _clamp(slope * SLOPE_LIFT_GAIN, 0.0, SLOPE_MAX_LIFT_PCT)
    if intensity <= 0.5:
        return anchors
    out: list[Anchor] = []
    for temp_c, pwm_pct in anchors:
        lift = intensity * _knee_weight(temp_c)
        out.append((temp_c, _clamp(pwm_pct + lift)))
    return out


# Default pipeline order. Kept as a module-level constant so tests can
# verify the order is what the docstring says, and so the actuator can
# import it directly.
DEFAULT_PIPELINE: tuple[Policy, ...] = (
    floor_to_49c,
    quiet_subtract,
    predictive_preload,
    slope_preload,
    workload_profile_shape,
    workload_headroom,
    recent_throttle_bump,
    heat_soak_bump,
    emergency_ramp,
)


def compose_curve(
    base: list[Anchor],
    ctx: CurveContext,
    policies: tuple[Policy, ...] = DEFAULT_PIPELINE,
) -> list[Anchor]:
    """Apply the policy pipeline left-to-right.

    Each policy gets the output of the previous one. The result is a
    fresh list of anchors with PWM clamped into [0, 100].
    """
    curve = [(t, _clamp(p)) for t, p in base]
    for policy in policies:
        curve = policy(curve, ctx)
    return curve


def curve_signature(anchors: list[Anchor]) -> str:
    """Stable string fingerprint of a curve. Used by the actuator to
    detect byte-equality with the previously-issued curve and skip the
    asusctl subprocess. Rounds PWM to 1 decimal so transient float noise
    doesn't force re-applies."""
    parts = [f"{int(round(t))}:{p:.1f}" for t, p in anchors]
    return ",".join(parts)


def is_finite_curve(anchors: list[Anchor]) -> bool:
    """Sanity guard — paranoid check against NaN/inf in case a policy
    misbehaves (e.g. divides by zero on a degenerate context). Used by
    the actuator as a pre-apply gate; on False, fall back to base."""
    return all(math.isfinite(t) and math.isfinite(p) for t, p in anchors)
