"""EWMA hysteresis for PWM output smoothing — P2.5 Heavy-4.

The composed fan curve oscillates anchor-by-anchor when the predictor's
suggested bias flickers (e.g. confidence shoulders the boundary between
two KNN clusters). Hardware vendors smooth this with hysteresis in
firmware; we don't have access to the BIOS controller, so we smooth at
the policy edge instead.

Design:

* Per-anchor exponential moving average. Each anchor temperature keeps
  its own «previous emitted PWM» state, so a single oscillating knee
  doesn't drag the whole curve.
* Alpha adapts to **predictor confidence**: when KNN is sure of what
  it's seeing (confidence → 1), the filter steps fast (α → 0.9). When
  confidence is low (predictor cold or unfamiliar workload), the
  filter holds steady (α → 0.2) — we trust history over a noisy
  signal.
* ``reset()`` drops state. Wired by Light-2 when the user flips
  operational mode or when the curve context changes meaningfully
  (game-mode toggle, profile flip).

``alpha = min_alpha + (max_alpha - min_alpha) * clamp(confidence, 0, 1)``

so ``confidence=0`` → α=0.2 (slow), ``confidence=1`` → α=0.9 (fast).
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


@dataclass(slots=True)
class EwmaFilter:
    """Per-anchor PWM smoothing. Each anchor remembers its last
    emitted value; the filter blends new target with previous via
    alpha that adapts to predictor confidence.

    Higher confidence → faster reaction (alpha → 0.9).
    Lower confidence → slower (alpha → 0.2)."""

    min_alpha: float = 0.2
    max_alpha: float = 0.9
    # Per-anchor state: temperature -> last emitted PWM. Keyed by
    # temperature so anchor reordering between calls (curve pipeline
    # policies sometimes reshuffle) doesn't lose history.
    _prev: dict[float, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_alpha <= self.max_alpha <= 1.0:
            raise ValueError(
                f"invalid alpha bounds: min={self.min_alpha} "
                f"max={self.max_alpha} (require 0 <= min <= max <= 1)"
            )

    def smooth(
        self,
        target_anchors: list[tuple[float, float]],
        confidence: float,
    ) -> list[tuple[float, float]]:
        """Return the per-anchor blended curve.

        State is mutated: the smoothed values become the «previous»
        for the next call. First call for a given anchor temperature
        emits the target unchanged (no history to blend against)."""
        alpha = self.min_alpha + (self.max_alpha - self.min_alpha) * _clamp(
            float(confidence), 0.0, 1.0
        )
        out: list[tuple[float, float]] = []
        for temp, target_pwm in target_anchors:
            t = float(temp)
            tgt = float(target_pwm)
            prev = self._prev.get(t)
            smoothed = tgt if prev is None else alpha * tgt + (1.0 - alpha) * prev
            self._prev[t] = smoothed
            out.append((t, smoothed))
        return out

    def reset(self) -> None:
        """Drop history. Called when the user flips mode or the curve
        context changes meaningfully (Light 2 will wire this)."""
        self._prev.clear()
