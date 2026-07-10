"""batch-defer probe — external compute-batch owner sentinel.

Symmetric to the game-mode defer: when an external owner (the off-hours
`reading-batch-power` script) is running, it touches a sentinel file and
coolstep's CPU-fan actuators stand down. These tests cover the probe helper
in `_base.py` in isolation; the actuator-level wiring is covered in
`test_asusctl_fan_curve.py` and `test_game_mode_optimizer.py`.
"""

from __future__ import annotations

from coolstep.adapters.actuators._base import (
    DEFAULT_BATCH_DEFER_FILE,
    _batch_defer_path,
    is_batch_defer_active,
)


def test_inactive_when_flag_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER", raising=False)
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER_PATH", raising=False)
    assert is_batch_defer_active() is False


def test_active_when_flag_present(monkeypatch, tmp_path):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER", raising=False)
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER_PATH", raising=False)
    (tmp_path / DEFAULT_BATCH_DEFER_FILE).write_text("")
    assert is_batch_defer_active() is True


def test_default_path_lives_in_coolstep_home(monkeypatch, tmp_path):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER_PATH", raising=False)
    assert _batch_defer_path() == tmp_path / DEFAULT_BATCH_DEFER_FILE


def test_explicit_path_override(monkeypatch, tmp_path):
    flag = tmp_path / "elsewhere" / "my.flag"
    flag.parent.mkdir(parents=True)
    monkeypatch.setenv("COOLSTEP_BATCH_DEFER_PATH", str(flag))
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER", raising=False)
    assert _batch_defer_path() == flag
    assert is_batch_defer_active() is False
    flag.write_text("")
    assert is_batch_defer_active() is True


def test_kill_switch_disables_probe(monkeypatch, tmp_path):
    """COOLSTEP_BATCH_DEFER=0 → probe is off even with the flag present."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER_PATH", raising=False)
    (tmp_path / DEFAULT_BATCH_DEFER_FILE).write_text("")
    monkeypatch.setenv("COOLSTEP_BATCH_DEFER", "0")
    assert is_batch_defer_active() is False
    monkeypatch.setenv("COOLSTEP_BATCH_DEFER", "false")
    assert is_batch_defer_active() is False
