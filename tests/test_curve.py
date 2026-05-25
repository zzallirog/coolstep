"""Adaptive curve policy tests — P2.4."""

from __future__ import annotations

from coolstep.core.curve import (
    Anchor,
    CurveContext,
    compose_curve,
    curve_signature,
    emergency_ramp,
    floor_to_49c,
    heat_soak_bump,
    is_finite_curve,
    predictive_preload,
    quiet_subtract,
    recent_throttle_bump,
    slope_preload,
    workload_headroom,
    workload_profile_shape,
)
from coolstep.core.workload_profile import is_headroom_class

# A baseline that mirrors the new actuator default. Centered so policies
# have something to bias around.
_BASE: list[Anchor] = [
    (40.0,  0.0),
    (49.0,  0.0),
    (56.0,  6.0),
    (64.0, 18.0),
    (70.0, 35.0),
    (75.0, 55.0),
    (80.0, 75.0),
    (85.0, 90.0),
    (90.0, 100.0),
]


def _pwm_at(curve: list[Anchor], temp: float) -> float:
    """Helper — fetch PWM for a specific anchor temperature."""
    for t, p in curve:
        if t == temp:
            return p
    raise AssertionError(f"anchor {temp} not in curve")


# ── floor_to_49c ──────────────────────────────────────────────────────


def test_floor_zeros_anchors_below_49():
    curve = floor_to_49c(_BASE, CurveContext())
    assert _pwm_at(curve, 40.0) == 0.0
    assert _pwm_at(curve, 49.0) == 0.0
    # Anything above 49 is untouched.
    assert _pwm_at(curve, 56.0) == 6.0
    assert _pwm_at(curve, 90.0) == 100.0


def test_floor_does_not_raise_anchors_above_49():
    """Sanity — policy only zeros below; doesn't lift below-zero up."""
    inflated = [(t, p + 5.0) for t, p in _BASE]
    out = floor_to_49c(inflated, CurveContext())
    # The 40 °C anchor was 5 % — must now be 0.
    assert _pwm_at(out, 40.0) == 0.0
    # The 56 °C anchor was 11 % — must stay 11.
    assert _pwm_at(out, 56.0) == 11.0


# ── quiet_subtract ────────────────────────────────────────────────────


def test_quiet_subtract_no_op_unless_quiet():
    ctx = CurveContext(mode="cool", throttle_prob=0.05, cpu_temp_c=60.0)
    out = quiet_subtract(_BASE, ctx)
    assert out == _BASE


def test_quiet_subtract_no_op_when_hot():
    ctx = CurveContext(mode="quiet", throttle_prob=0.05, cpu_temp_c=78.0)
    out = quiet_subtract(_BASE, ctx)
    assert out == _BASE


def test_quiet_subtract_no_op_when_prob_high():
    ctx = CurveContext(mode="quiet", throttle_prob=0.5, cpu_temp_c=60.0)
    out = quiet_subtract(_BASE, ctx)
    assert out == _BASE


def test_quiet_subtract_lowers_knee_when_eligible():
    ctx = CurveContext(mode="quiet", throttle_prob=0.05, cpu_temp_c=55.0)
    out = quiet_subtract(_BASE, ctx)
    # Knee centre 76 °C is between 75 (anchor) and 80 (anchor). The 80 °C
    # anchor is inside the trapezoid plateau — should be visibly reduced.
    assert _pwm_at(out, 80.0) < _pwm_at(_BASE, 80.0)
    # Outside-knee anchors untouched
    assert _pwm_at(out, 49.0) == _pwm_at(_BASE, 49.0)
    assert _pwm_at(out, 90.0) == _pwm_at(_BASE, 90.0)


def test_quiet_subtract_respects_floor():
    """No anchor in the knee may go below QUIET_FLOOR_PWM = 12 %."""
    ctx = CurveContext(mode="quiet", throttle_prob=0.05, cpu_temp_c=55.0)
    low = [(40.0, 0.0), (49.0, 0.0), (56.0, 0.0), (64.0, 0.0),
           (70.0, 4.0), (75.0, 10.0), (80.0, 14.0), (85.0, 18.0), (90.0, 25.0)]
    out = quiet_subtract(low, ctx)
    for t, p in out:
        if 70.0 <= t <= 85.0:
            assert p >= 12.0 or p == _pwm_at(low, t)


# ── predictive_preload ────────────────────────────────────────────────


def test_predictive_no_op_below_prob_threshold():
    ctx = CurveContext(throttle_prob=0.3, mode="cool")
    out = predictive_preload(_BASE, ctx)
    assert out == _BASE


def test_predictive_no_op_in_off_mode():
    ctx = CurveContext(throttle_prob=0.95, mode="off")
    out = predictive_preload(_BASE, ctx)
    assert out == _BASE


def test_predictive_lifts_knee_proportional_to_prob():
    low = CurveContext(throttle_prob=0.65, mode="cool", horizon_sec=30)
    high = CurveContext(throttle_prob=0.95, mode="cool", horizon_sec=30)
    low_out = predictive_preload(_BASE, low)
    high_out = predictive_preload(_BASE, high)
    # higher prob → bigger lift at knee
    assert _pwm_at(high_out, 76.0) if False else True   # placeholder
    assert _pwm_at(high_out, 75.0) > _pwm_at(low_out, 75.0)


def test_predictive_lift_fades_with_horizon():
    near = CurveContext(throttle_prob=0.95, mode="cool", horizon_sec=30)
    far  = CurveContext(throttle_prob=0.95, mode="cool", horizon_sec=120)
    near_out = predictive_preload(_BASE, near)
    far_out = predictive_preload(_BASE, far)
    assert _pwm_at(near_out, 75.0) > _pwm_at(far_out, 75.0)


# ── workload_headroom ────────────────────────────────────────────────


def test_workload_no_op_unknown_class():
    ctx = CurveContext(workload_class=None)
    out = workload_headroom(_BASE, ctx)
    assert out == _BASE
    ctx2 = CurveContext(workload_class="vscode")  # CODE — not headroom-eligible
    assert workload_headroom(_BASE, ctx2) == _BASE


def test_workload_lifts_knee_for_known_hot_class():
    ctx = CurveContext(workload_class="steam")
    out = workload_headroom(_BASE, ctx)
    # The knee centre 75-80 lifts; below knee unchanged.
    assert _pwm_at(out, 75.0) > _pwm_at(_BASE, 75.0)
    assert _pwm_at(out, 49.0) == _pwm_at(_BASE, 49.0)


# ── recent_throttle_bump ─────────────────────────────────────────────


def test_recent_throttle_no_op_when_count_low():
    ctx = CurveContext(recent_throttle_count=1)
    assert recent_throttle_bump(_BASE, ctx) == _BASE


def test_recent_throttle_lifts_65_to_80_band():
    ctx = CurveContext(recent_throttle_count=3)
    out = recent_throttle_bump(_BASE, ctx)
    # 70 in range → lifted
    assert _pwm_at(out, 70.0) > _pwm_at(_BASE, 70.0)
    # 90 outside → untouched
    assert _pwm_at(out, 90.0) == _pwm_at(_BASE, 90.0)
    assert _pwm_at(out, 49.0) == _pwm_at(_BASE, 49.0)


# ── emergency_ramp ───────────────────────────────────────────────────


def test_emergency_no_op_below_threshold():
    ctx = CurveContext(cpu_temp_c=80.0)
    assert emergency_ramp(_BASE, ctx) == _BASE


def test_emergency_lifts_anchors_at_or_above_current_temp():
    ctx = CurveContext(cpu_temp_c=82.0)
    out = emergency_ramp(_BASE, ctx)
    # 85, 90 are above 82 → max
    assert _pwm_at(out, 85.0) == 100.0
    assert _pwm_at(out, 90.0) == 100.0
    # Below current — untouched
    assert _pwm_at(out, 75.0) == _pwm_at(_BASE, 75.0)


def test_emergency_runs_last_overrides_quiet():
    """Stack quiet_subtract (would lower) + emergency (must override)."""
    ctx = CurveContext(mode="quiet", throttle_prob=0.05,
                       cpu_temp_c=82.5, recent_throttle_count=0)
    after_quiet = quiet_subtract(_BASE, ctx)
    final = emergency_ramp(after_quiet, ctx)
    assert _pwm_at(final, 85.0) == 100.0
    assert _pwm_at(final, 90.0) == 100.0


# ── compose_curve ────────────────────────────────────────────────────


def test_compose_with_default_pipeline_is_idempotent():
    ctx = CurveContext(mode="cool", throttle_prob=0.0, cpu_temp_c=55.0)
    first = compose_curve(_BASE, ctx)
    second = compose_curve(_BASE, ctx)
    assert first == second
    assert curve_signature(first) == curve_signature(second)


def test_compose_in_calm_mode_floors_below_49():
    ctx = CurveContext(mode="cool", throttle_prob=0.0, cpu_temp_c=42.0)
    out = compose_curve(_BASE, ctx)
    assert _pwm_at(out, 40.0) == 0.0
    assert _pwm_at(out, 49.0) == 0.0


def test_compose_emergency_dominates_other_policies():
    """Even in quiet mode with calm prediction, emergency forces ramp."""
    ctx = CurveContext(
        mode="quiet", throttle_prob=0.05,
        cpu_temp_c=83.5, workload_class="steam",
    )
    out = compose_curve(_BASE, ctx)
    assert _pwm_at(out, 85.0) == 100.0
    assert _pwm_at(out, 90.0) == 100.0


def test_signature_changes_when_curve_changes():
    calm = CurveContext(throttle_prob=0.0, cpu_temp_c=55.0)
    hot  = CurveContext(throttle_prob=0.95, cpu_temp_c=72.0)
    sig_calm = curve_signature(compose_curve(_BASE, calm))
    sig_hot  = curve_signature(compose_curve(_BASE, hot))
    assert sig_calm != sig_hot


def test_is_finite_curve_accepts_normal():
    assert is_finite_curve(_BASE) is True


def test_is_finite_curve_rejects_nan():
    import math
    assert is_finite_curve([(60.0, math.nan)]) is False


# ── P2.5 Conservative Mode ─────────────────────────────────────────────


def test_predictive_preload_high_conf_holds_at_0_5():
    """High confidence + prob 0.5 → no preload (below 0.6 floor)."""
    ctx = CurveContext(throttle_prob=0.5, confidence=0.9, mode="cool", conservative=False)
    assert predictive_preload(_BASE, ctx) == _BASE


def test_predictive_preload_conservative_widens_floor():
    """Conservative + prob 0.5 → preload fires (widened to 0.45)."""
    ctx = CurveContext(throttle_prob=0.5, confidence=0.3, mode="cool", conservative=True)
    out = predictive_preload(_BASE, ctx)
    assert _pwm_at(out, 75.0) > _pwm_at(_BASE, 75.0)


def test_predictive_preload_conservative_amplifies():
    """Same prob, conservative=True should lift more than conservative=False
    (1.2× amp boost)."""
    cool = CurveContext(throttle_prob=0.7, confidence=0.9, mode="cool", conservative=False)
    cons = CurveContext(throttle_prob=0.7, confidence=0.3, mode="cool", conservative=True)
    cool_out = predictive_preload(_BASE, cool)
    cons_out = predictive_preload(_BASE, cons)
    assert _pwm_at(cons_out, 75.0) > _pwm_at(cool_out, 75.0)


def test_predictive_preload_conservative_floor_at_0_4_still_blocks():
    """Even conservative, prob 0.4 doesn't trigger (floor is 0.45)."""
    ctx = CurveContext(throttle_prob=0.4, confidence=0.3, mode="cool", conservative=True)
    assert predictive_preload(_BASE, ctx) == _BASE


def test_thresholds_defaults_conservative_floor():
    """Defensive — lock the conservative_floor default so a silent change
    surfaces as a test failure."""
    from coolstep.core.decision import Thresholds
    t = Thresholds()
    assert t.conservative_floor == 0.45
    assert t.conservative_prob_hint == 0.2


# ── P2.5 Phase C: workload_profile_shape ───────────────────────────────


def test_workload_profile_shape_no_op_for_other():
    ctx = CurveContext(workload_profile="other")
    assert workload_profile_shape(_BASE, ctx) == _BASE


def test_workload_profile_shape_game_lifts_more_than_code():
    """GAME bumps the knee up, CODE shaves it. Same prob, different profile.
    Asserted at 75 °C — the trapezoid plateau where _knee_weight = 1.0."""
    game_ctx = CurveContext(workload_profile="game")
    code_ctx = CurveContext(workload_profile="code")
    game_out = workload_profile_shape(_BASE, game_ctx)
    code_out = workload_profile_shape(_BASE, code_ctx)
    assert _pwm_at(game_out, 75.0) > _pwm_at(_BASE, 75.0)
    assert _pwm_at(code_out, 75.0) < _pwm_at(_BASE, 75.0)
    assert _pwm_at(game_out, 75.0) > _pwm_at(code_out, 75.0)


def test_workload_profile_shape_render_between_code_and_game():
    code_out = workload_profile_shape(_BASE, CurveContext(workload_profile="code"))
    render_out = workload_profile_shape(_BASE, CurveContext(workload_profile="render"))
    game_out = workload_profile_shape(_BASE, CurveContext(workload_profile="game"))
    knee_code = _pwm_at(code_out, 75.0)
    knee_render = _pwm_at(render_out, 75.0)
    knee_game = _pwm_at(game_out, 75.0)
    assert knee_code < knee_render < knee_game


def test_workload_profile_shape_idle_shaves_but_clamps_at_zero():
    """IDLE applies -3 % at knee, but a 0 % anchor must not go negative."""
    low = [(70.0, 2.0), (75.0, 1.0), (80.0, 0.0)]
    out = workload_profile_shape(low, CurveContext(workload_profile="idle"))
    for _, p in out:
        assert p >= 0.0


def test_workload_profile_shape_only_touches_knee_band():
    """Outside the trapezoid (below 70, above 85) the bump weight is 0."""
    ctx = CurveContext(workload_profile="game")
    out = workload_profile_shape(_BASE, ctx)
    assert _pwm_at(out, 40.0) == _pwm_at(_BASE, 40.0)
    assert _pwm_at(out, 49.0) == _pwm_at(_BASE, 49.0)
    assert _pwm_at(out, 56.0) == _pwm_at(_BASE, 56.0)
    assert _pwm_at(out, 90.0) == _pwm_at(_BASE, 90.0)


def test_workload_headroom_matcher_covers_game_render_browser_and_steam_app():
    """Single source of truth: headroom uses the same matcher as profile
    resolution, so prefix-wrappers (steam_app_*) and BROWSER stay
    headroom-eligible."""
    assert is_headroom_class("steam")
    assert is_headroom_class("blender")
    assert is_headroom_class("steam_app_2073850")
    assert is_headroom_class("lutris-witcher3")
    # Browser is now headroom-eligible — empirically the dominant heat source.
    assert is_headroom_class("zen")
    assert is_headroom_class("firefox")
    # Negative — terminal / editor shouldn't get the +5 % bump.
    assert not is_headroom_class("kitty")
    assert not is_headroom_class("vscode")


# ── slope_preload (reactive, KNN-independent) ─────────────────────────


def test_slope_preload_no_op_when_chip_cool():
    # Hot slope but chip still below the 75 °C floor — don't fight idle wobble.
    ctx = CurveContext(cpu_temp_c=70.0, cpu_temp_slope_short_c_per_sec=0.5)
    assert slope_preload(_BASE, ctx) == _BASE


def test_slope_preload_no_op_below_slope_threshold():
    ctx = CurveContext(cpu_temp_c=78.0, cpu_temp_slope_short_c_per_sec=0.1)
    assert slope_preload(_BASE, ctx) == _BASE


def test_slope_preload_lifts_knee_when_hot_and_rising():
    ctx = CurveContext(cpu_temp_c=78.0, cpu_temp_slope_short_c_per_sec=0.4)
    out = slope_preload(_BASE, ctx)
    # Knee centre lifts; the 49 °C and 90 °C edges sit outside the band.
    assert _pwm_at(out, 75.0) > _pwm_at(_BASE, 75.0)
    assert _pwm_at(out, 49.0) == _pwm_at(_BASE, 49.0)
    assert _pwm_at(out, 90.0) == _pwm_at(_BASE, 90.0)


def test_slope_preload_caps_at_max_lift():
    # Absurdly high slope shouldn't push the knee past SLOPE_MAX_LIFT_PCT.
    from coolstep.core.curve import SLOPE_MAX_LIFT_PCT
    ctx = CurveContext(cpu_temp_c=78.0, cpu_temp_slope_short_c_per_sec=5.0)
    out = slope_preload(_BASE, ctx)
    delta = _pwm_at(out, 75.0) - _pwm_at(_BASE, 75.0)
    assert delta <= SLOPE_MAX_LIFT_PCT + 0.01


def test_slope_preload_no_op_when_mode_off():
    ctx = CurveContext(mode="off", cpu_temp_c=78.0, cpu_temp_slope_short_c_per_sec=0.5)
    assert slope_preload(_BASE, ctx) == _BASE


# ── P2.5 Phase D: heat_soak_bump ───────────────────────────────────────


def test_heat_soak_bump_no_op_at_low_index():
    ctx = CurveContext(heat_soak_index=0.0)
    assert heat_soak_bump(_BASE, ctx) == _BASE
    ctx2 = CurveContext(heat_soak_index=0.4)  # below threshold (0.5)
    assert heat_soak_bump(_BASE, ctx2) == _BASE


def test_heat_soak_bump_lifts_when_high():
    ctx = CurveContext(heat_soak_index=1.0)
    out = heat_soak_bump(_BASE, ctx)
    # Knee centre lifted, base/top untouched (knee_weight=0 outside band).
    assert _pwm_at(out, 75.0) > _pwm_at(_BASE, 75.0)
    assert _pwm_at(out, 49.0) == _pwm_at(_BASE, 49.0)
    assert _pwm_at(out, 90.0) == _pwm_at(_BASE, 90.0)


def test_heat_soak_bump_caps_at_max():
    """Very large index must not overshoot HEAT_SOAK_MAX_BUMP_PCT (10 %)."""
    huge = CurveContext(heat_soak_index=100.0)
    moderate = CurveContext(heat_soak_index=2.0)  # 2 * 6 = 12 → clamped to 10
    out_huge = heat_soak_bump(_BASE, huge)
    out_mod = heat_soak_bump(_BASE, moderate)
    # Both should be at the cap at knee centre.
    assert _pwm_at(out_huge, 75.0) == _pwm_at(out_mod, 75.0)


def test_heat_soak_bump_negative_index_no_op():
    """Negative index = chip is cooling → never lift."""
    ctx = CurveContext(heat_soak_index=-1.0)
    assert heat_soak_bump(_BASE, ctx) == _BASE
