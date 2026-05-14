"""EWMA hysteresis tests — P2.5 Heavy-4.

Covers:
- alpha math: confidence=0 → min_alpha, confidence=1 → max_alpha,
  linear interpolation in between, clamping at ends
- state mutation: per-anchor previous value persists across calls
- first call (no history) emits target unchanged
- reset() clears the history dict
- anchor reordering between calls is safe (state keyed by temperature)
- invalid bounds raise ValueError
"""

from __future__ import annotations

import math

import pytest

from coolstep.core.ewma_filter import EwmaFilter


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── construction / validation ────────────────────────────────────────


def test_default_alpha_bounds():
    f = EwmaFilter()
    assert f.min_alpha == 0.2
    assert f.max_alpha == 0.9


def test_custom_alpha_bounds():
    f = EwmaFilter(min_alpha=0.1, max_alpha=0.5)
    assert f.min_alpha == 0.1
    assert f.max_alpha == 0.5


def test_invalid_alpha_bounds_raise():
    # min > max
    with pytest.raises(ValueError):
        EwmaFilter(min_alpha=0.5, max_alpha=0.2)
    # max > 1
    with pytest.raises(ValueError):
        EwmaFilter(min_alpha=0.1, max_alpha=1.5)
    # min < 0
    with pytest.raises(ValueError):
        EwmaFilter(min_alpha=-0.1, max_alpha=0.5)


# ── first call: no history → passthrough ─────────────────────────────


def test_first_call_passthrough():
    f = EwmaFilter()
    out = f.smooth([(50.0, 30.0), (75.0, 60.0)], confidence=0.7)
    # No prior state — target emitted unchanged
    assert out == [(50.0, 30.0), (75.0, 60.0)]


# ── alpha math at boundaries ─────────────────────────────────────────


def test_alpha_at_zero_confidence():
    """confidence=0 → alpha = min_alpha = 0.2"""
    f = EwmaFilter(min_alpha=0.2, max_alpha=0.9)
    # Seed
    f.smooth([(50.0, 30.0)], confidence=0.0)
    # Second call: smoothed = 0.2 * 60 + 0.8 * 30 = 12 + 24 = 36
    out = f.smooth([(50.0, 60.0)], confidence=0.0)
    assert _close(out[0][1], 36.0)


def test_alpha_at_full_confidence():
    """confidence=1 → alpha = max_alpha = 0.9"""
    f = EwmaFilter(min_alpha=0.2, max_alpha=0.9)
    f.smooth([(50.0, 30.0)], confidence=1.0)
    # Second call: smoothed = 0.9 * 60 + 0.1 * 30 = 54 + 3 = 57
    out = f.smooth([(50.0, 60.0)], confidence=1.0)
    assert _close(out[0][1], 57.0)


def test_alpha_midpoint():
    """confidence=0.5 → alpha = 0.2 + 0.5*(0.9-0.2) = 0.55"""
    f = EwmaFilter(min_alpha=0.2, max_alpha=0.9)
    f.smooth([(50.0, 30.0)], confidence=0.5)
    # smoothed = 0.55 * 60 + 0.45 * 30 = 33 + 13.5 = 46.5
    out = f.smooth([(50.0, 60.0)], confidence=0.5)
    assert _close(out[0][1], 46.5)


def test_confidence_clamped_below_zero():
    f = EwmaFilter()
    f.smooth([(50.0, 30.0)], confidence=-2.0)
    # Clamped → alpha = min_alpha = 0.2
    out = f.smooth([(50.0, 60.0)], confidence=-2.0)
    assert _close(out[0][1], 0.2 * 60 + 0.8 * 30)


def test_confidence_clamped_above_one():
    f = EwmaFilter()
    f.smooth([(50.0, 30.0)], confidence=3.0)
    # Clamped → alpha = max_alpha = 0.9
    out = f.smooth([(50.0, 60.0)], confidence=3.0)
    assert _close(out[0][1], 0.9 * 60 + 0.1 * 30)


# ── per-anchor independence ──────────────────────────────────────────


def test_per_anchor_state():
    """Each anchor temperature has its own history."""
    f = EwmaFilter()
    f.smooth([(50.0, 30.0), (75.0, 60.0)], confidence=1.0)
    # Second call updates each independently
    out = f.smooth([(50.0, 40.0), (75.0, 70.0)], confidence=1.0)
    # alpha = 0.9 for both
    assert _close(out[0][1], 0.9 * 40 + 0.1 * 30)
    assert _close(out[1][1], 0.9 * 70 + 0.1 * 60)


def test_anchor_reordering_preserves_state():
    """Same anchor temps in different order still find their prior."""
    f = EwmaFilter()
    f.smooth([(50.0, 30.0), (75.0, 60.0)], confidence=1.0)
    # Reorder
    out = f.smooth([(75.0, 70.0), (50.0, 40.0)], confidence=1.0)
    # 75 anchor: prev=60, target=70 → 0.9*70 + 0.1*60 = 69
    # 50 anchor: prev=30, target=40 → 0.9*40 + 0.1*30 = 39
    assert _close(out[0][1], 69.0)
    assert _close(out[1][1], 39.0)


# ── state mutation across many calls ─────────────────────────────────


def test_repeated_calls_converge_to_target():
    """With a constant target and α=0.5, the output converges
    geometrically toward target. Sanity-check the EMA identity."""
    f = EwmaFilter(min_alpha=0.5, max_alpha=0.5)  # fixed α=0.5
    target = 80.0
    # Seed with a different value
    f.smooth([(70.0, 0.0)], confidence=0.5)
    last = 0.0
    for _ in range(20):
        out = f.smooth([(70.0, target)], confidence=0.5)
        last = out[0][1]
    # After 20 steps with α=0.5, residual is (0.5^20) * |80 - 0| ≈ 7.6e-5
    assert abs(last - target) < 1e-3


# ── reset() ──────────────────────────────────────────────────────────


def test_reset_clears_history():
    f = EwmaFilter()
    f.smooth([(50.0, 30.0)], confidence=1.0)
    f.reset()
    # Next call should pass through (no history)
    out = f.smooth([(50.0, 99.0)], confidence=1.0)
    assert out == [(50.0, 99.0)]


def test_reset_on_empty_filter_is_noop():
    f = EwmaFilter()
    f.reset()  # no error
    out = f.smooth([(50.0, 30.0)], confidence=0.5)
    assert out == [(50.0, 30.0)]
