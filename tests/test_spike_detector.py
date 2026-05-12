"""Tests for SpikeDetector — entry/exit debounce, closure semantics."""

from __future__ import annotations

import pytest

from coolstep.core.spike_detector import SpikeDetector, SpikeRecord


def _feed(det: SpikeDetector, residuals: list[float], *, start_ts: float = 0.0,
          step: float = 1.0, peak_temp: float = 70.0, workload: str | None = "speedtest",
          model: str = "trajectory+meta") -> list[SpikeRecord | None]:
    """Push a residual sequence at 1 Hz default. Returns the list of
    detector outputs (None for non-closure ticks, SpikeRecord on closure)."""
    out: list[SpikeRecord | None] = []
    for i, r in enumerate(residuals):
        ts = start_ts + i * step
        out.append(det.update(
            ts=ts,
            residual_c=r,
            peak_temp_c=peak_temp,
            workload_label=workload,
            features={"cpu_temp_max": peak_temp, "cpu_load_max": 90.0},
            predictor_model=model,
        ))
    return out


def test_idle_below_threshold_emits_nothing():
    det = SpikeDetector()
    out = _feed(det, [0.5, 1.2, 0.0, -1.8, 0.3, 0.0])
    assert all(o is None for o in out)
    assert det.live_state()["active"] is False


def test_single_tick_high_does_not_open_spike():
    """A single noisy frame at |res|=6° must NOT open a spike — the
    entry_n debounce (default 2) is exactly what guards against this."""
    det = SpikeDetector()
    out = _feed(det, [6.0, 0.5, 0.5, 0.5, 0.5])
    assert all(o is None for o in out)
    assert det.live_state()["active"] is False


def test_two_consecutive_high_opens_spike():
    det = SpikeDetector()
    _feed(det, [6.0, 6.0])
    st = det.live_state()
    assert st["active"] is True
    assert st["started_at"] == 1.0  # 2nd tick is the opener
    assert st["max_abs_residual"] == 6.0
    assert st["workload_label"] == "speedtest"


def test_spike_tracks_max_and_count_until_exit():
    """Operator scenario: predictor overshoots by 8° on tick 1-2 of a
    new workload, drifts 4-6° for a few ticks, then settles.  The
    record should capture the worst residual the spike saw."""
    det = SpikeDetector()
    out = _feed(det, [
        7.0, 8.5,         # entry (n=2)
        9.0, 5.0, 3.0,    # in-spike, max climbs to 9
        1.5, 1.5, 1.5,    # 3 consecutive < exit_thresh → close
        0.5,              # idle
    ])
    closures = [o for o in out if o is not None]
    assert len(closures) == 1
    rec = closures[0]
    # Entry at ts=1.0 (2nd tick of the high streak), close at ts=7.0
    # (3rd consecutive low) → duration 6.0.
    assert rec.duration_s == pytest.approx(6.0)
    assert rec.max_abs_residual == 9.0
    # 2 entry ticks (seeded into n_validations) + 6 active ticks (2..7)
    assert rec.n_validations == 8
    assert rec.workload_label == "speedtest"
    assert rec.predictor_model == "trajectory+meta"
    assert "cpu_temp_max" in rec.started_features
    # After closure the detector is reset.
    assert det.live_state()["active"] is False


def test_low_streak_interruption_does_not_close_early():
    """If we go low for 1 tick then high again, the exit_n=3 debounce
    must NOT count the interrupted streak."""
    det = SpikeDetector()
    out = _feed(det, [
        7.0, 7.0,        # entry
        1.0, 1.0,        # 2 low ticks
        6.0,             # back up — exit streak resets
        1.0, 1.0, 1.0,   # 3 low → close
    ])
    closures = [o for o in out if o is not None]
    assert len(closures) == 1
    rec = closures[0]
    # The "back up" tick should be inside the spike, so max stays at 7.0
    # and duration covers the full episode (open ts=1.0 → close ts=7.0).
    assert rec.max_abs_residual == 7.0
    assert rec.duration_s == pytest.approx(6.0)


def test_negative_residuals_count_by_magnitude():
    """A predictor that *under*-shoots by 6° is just as much a spike as
    one that overshoots — abs() is the right metric."""
    det = SpikeDetector()
    out = _feed(det, [-7.0, -7.0, -1.5, -1.5, -1.5])
    closures = [o for o in out if o is not None]
    assert len(closures) == 1
    assert closures[0].max_abs_residual == 7.0


def test_two_separate_spikes_emit_two_records():
    det = SpikeDetector()
    out = _feed(det, [
        7.0, 7.0,            # spike 1 entry
        1.0, 1.0, 1.0,       # spike 1 exit
        0.5, 0.5,            # gap
        8.0, 8.0,            # spike 2 entry
        1.0, 1.0, 1.0,       # spike 2 exit
    ])
    closures = [o for o in out if o is not None]
    assert len(closures) == 2
    assert closures[0].max_abs_residual == 7.0
    assert closures[1].max_abs_residual == 8.0


def test_constructor_rejects_inverted_thresholds():
    with pytest.raises(ValueError):
        SpikeDetector(entry_thresh_c=2.0, exit_thresh_c=5.0)


def test_constructor_rejects_zero_debounce():
    with pytest.raises(ValueError):
        SpikeDetector(entry_n=0)
    with pytest.raises(ValueError):
        SpikeDetector(exit_n=0)


def test_live_state_includes_thresholds_for_ui_legend():
    """Dashboard needs to display entry/exit thresholds so the operator
    can read a "3.7°" residual and know whether the detector cares."""
    det = SpikeDetector(entry_thresh_c=5.0, exit_thresh_c=2.0)
    st = det.live_state()
    assert st["thresholds"]["entry_c"] == 5.0
    assert st["thresholds"]["exit_c"] == 2.0
