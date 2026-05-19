"""game_mode_optimizer actuator tests — P2.5 ADR-015.

Mocks `systemctl --user is-active game-mode.service` AND `asusctl`.
"""

from __future__ import annotations

import subprocess
import time
from unittest.mock import patch

from coolstep.adapters.actuators.game_mode_optimizer import (
    GAME_PROFILE_ANCHORS,
    GameModeOptimizer,
    _format_data_arg,
    make,
)
from coolstep.core.schema import Action, ActionVerb


def _completed(
    stdout: bytes = b"", returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=b"",
    )


def _gm_inactive() -> subprocess.CompletedProcess[bytes]:
    return _completed(stdout=b"inactive\n", returncode=3)


def _gm_active() -> subprocess.CompletedProcess[bytes]:
    return _completed(stdout=b"active\n", returncode=0)


# ── supports() truth table ────────────────────────────────────────────────


def test_supports_false_when_game_mode_inactive(monkeypatch):
    """inactive game-mode → False (asusctl_fan_curve_bias owns)."""
    monkeypatch.delenv("COOLSTEP_GAME_MODE_DEFER", raising=False)
    a = GameModeOptimizer()
    with patch("subprocess.run", return_value=_gm_inactive()):
        assert a.supports(ActionVerb.RAMP_COOLING) is False


def test_supports_true_when_game_mode_active_with_defer(monkeypatch):
    """active + DEFER=1 (default) → True (this actuator owns)."""
    monkeypatch.setenv("COOLSTEP_GAME_MODE_DEFER", "1")
    a = GameModeOptimizer()
    with patch("subprocess.run", return_value=_gm_active()):
        assert a.supports(ActionVerb.RAMP_COOLING) is True


def test_supports_false_when_game_mode_active_with_cooperative(monkeypatch):
    """active + DEFER=0 → False (asusctl_fan_curve_bias owns, cooperative)."""
    monkeypatch.setenv("COOLSTEP_GAME_MODE_DEFER", "0")
    a = GameModeOptimizer()
    with patch("subprocess.run", return_value=_gm_active()):
        assert a.supports(ActionVerb.RAMP_COOLING) is False


def test_supports_other_verbs_always_false(monkeypatch):
    """NOTIFY_USER / CAP_BOOST → False even with game-mode active + defer on."""
    monkeypatch.setenv("COOLSTEP_GAME_MODE_DEFER", "1")
    a = GameModeOptimizer()
    with patch("subprocess.run") as mock_run:
        assert a.supports(ActionVerb.NOTIFY_USER) is False
        assert a.supports(ActionVerb.CAP_BOOST) is False
        assert a.supports(ActionVerb.SHIFT_POWER_ENVELOPE) is False
    # Non-RAMP_COOLING verbs short-circuit before any systemctl probe.
    assert mock_run.call_count == 0


# ── apply() ──────────────────────────────────────────────────────────────


def test_apply_dryrun_logs_cmd_with_game_anchors(monkeypatch):
    """Default mode → cmd_executed contains the 73c:50% game-profile knee."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = GameModeOptimizer()
    action = Action(
        verb=ActionVerb.RAMP_COOLING, params={},
        expires_at=time.time() + 30.0,
    )
    with patch("subprocess.run") as mock_run:
        result = a.apply(action)
    # Dry-run never calls subprocess.
    assert mock_run.call_count == 0
    assert result.error is None
    assert result.cmd_executed is not None
    assert "--data" in result.cmd_executed
    assert "73c:50%" in result.cmd_executed  # the game-profile knee
    assert result.stdout_tail == "DRY-RUN game-profile"


def test_apply_armed_invokes_subprocess(monkeypatch):
    """ARMED → subprocess.run called with --data + the game-profile anchors."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = GameModeOptimizer()
    action = Action(
        verb=ActionVerb.RAMP_COOLING, params={},
        expires_at=time.time() + 30.0,
    )
    seen: list[list[str]] = []

    def fake_run(cmd, **_kw):
        seen.append(list(cmd))
        return _completed(stdout=b"ok", returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)
    assert result.error is None
    assert len(seen) == 1
    cmd = seen[0]
    assert "fan-curve" in cmd
    assert "--mod-profile" in cmd
    assert "--data" in cmd
    data_arg = cmd[cmd.index("--data") + 1]
    # Spot-check the canonical game anchors land in --data.
    assert "73c:50%" in data_arg
    assert "76c:70%" in data_arg
    assert "90c:100%" in data_arg


def test_apply_handles_subprocess_failure(monkeypatch):
    """CalledProcessError → result.error contains 'returncode='."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "1")
    a = GameModeOptimizer()
    action = Action(
        verb=ActionVerb.RAMP_COOLING, params={},
        expires_at=time.time() + 30.0,
    )

    def fake_run(cmd, **_kw):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr=b"asusctl: profile not active",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)
    assert result.error is not None
    assert "returncode=1" in result.error


# ── revert() ─────────────────────────────────────────────────────────────


def test_revert_dryrun_no_subprocess(monkeypatch):
    """Default mode → revert() doesn't call subprocess."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = GameModeOptimizer()
    with patch("subprocess.run") as mock_run:
        a.revert()
    assert mock_run.call_count == 0


def test_revert_armed_invokes_default(monkeypatch):
    """ARMED → revert calls --default for the active profile."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = GameModeOptimizer()
    with patch("subprocess.run", return_value=_completed(stdout=b"ok")) as mock_run:
        a.revert()
    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert "fan-curve" in cmd
    assert "--default" in cmd


# ── discovery ────────────────────────────────────────────────────────────


def test_make_returns_none_when_asusctl_missing(monkeypatch):
    """shutil.which → None → make() returns None (no-op on non-ASUS hosts)."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    with patch("shutil.which", return_value=None):
        assert make() is None


# ── extras: format helper + journal smoke ────────────────────────────────


def test_format_data_arg_shapes_game_anchors():
    """Round-trip the canonical game anchors through `_format_data_arg`."""
    s = _format_data_arg(GAME_PROFILE_ANCHORS)
    # Spot-check first / knee / last anchors.
    assert s.startswith("60c:5%")
    assert "73c:50%" in s
    assert s.endswith("90c:100%")
