"""notify_send actuator tests."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import patch

from coolstep.adapters.actuators.notify_send import NotifySendActuator, make
from coolstep.core.schema import Action, ActionVerb


def _action(
    verb: ActionVerb = ActionVerb.NOTIFY_USER,
    params: dict | None = None,
    expires: float = 9999.0,
) -> Action:
    return Action(verb=verb, params=params or {}, expires_at=expires)


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["notify-send"], returncode=returncode, stdout=b"", stderr=b"",
    )


# ── make() factory ──────────────────────────────────────────────────────────

def test_make_returns_none_when_binary_missing():
    with patch("shutil.which", return_value=None):
        assert make() is None


def test_make_returns_none_when_disabled_via_env(monkeypatch):
    monkeypatch.setenv("COOLSTEP_NOTIFY_ENABLE", "false")
    with patch("shutil.which", return_value="/usr/bin/notify-send"):
        assert make() is None


def test_make_returns_actuator_when_binary_present():
    with patch("shutil.which", return_value="/usr/bin/notify-send"):
        instance = make()
    assert isinstance(instance, NotifySendActuator)


# ── supports() ──────────────────────────────────────────────────────────────

def test_supports_only_notify_user():
    a = NotifySendActuator()
    assert a.supports(ActionVerb.NOTIFY_USER)
    assert not a.supports(ActionVerb.RAMP_COOLING)
    assert not a.supports(ActionVerb.CAP_BOOST)


# ── apply() normal path ──────────────────────────────────────────────────────

def test_apply_invokes_notify_send_with_message():
    a = NotifySendActuator(binary="notify-send")
    action = _action(params={"message": "hello thermal"})
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        result = a.apply(action)
    assert result.error is None
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "notify-send"
    assert "--app-name" in call_args
    assert "coolstep" in call_args
    assert "--urgency" in call_args
    assert "normal" in call_args
    assert "--icon" in call_args
    assert "weather-clear-warning" in call_args
    assert "hello thermal" in call_args


def test_apply_uses_urgency_from_params():
    a = NotifySendActuator(binary="notify-send")
    action = _action(params={"urgency": "critical", "message": "hot"})
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        a.apply(action)
    call_args = mock_run.call_args[0][0]
    urgency_idx = call_args.index("--urgency")
    assert call_args[urgency_idx + 1] == "critical"


def test_apply_clamps_invalid_urgency_to_normal():
    a = NotifySendActuator(binary="notify-send")
    action = _action(params={"urgency": "panic", "message": "bad"})
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        a.apply(action)
    call_args = mock_run.call_args[0][0]
    urgency_idx = call_args.index("--urgency")
    assert call_args[urgency_idx + 1] == "normal"


# ── cooldown ─────────────────────────────────────────────────────────────────

def test_apply_respects_cooldown():
    a = NotifySendActuator(binary="notify-send")
    # Pre-seed _last_fire so the first call is already in cooldown.
    a._last_fire[ActionVerb.NOTIFY_USER] = time.time()
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        result = a.apply(_action())
    assert result.stdout_tail.startswith("COOLDOWN")
    mock_run.assert_not_called()


# ── error paths ──────────────────────────────────────────────────────────────

def test_apply_handles_subprocess_failure():
    a = NotifySendActuator(binary="notify-send")
    exc = subprocess.CalledProcessError(1, ["notify-send"], stderr=b"session error")
    with patch("subprocess.run", side_effect=exc):
        result = a.apply(_action())
    assert result.error is not None
    assert "returncode=1" in result.error


# ── revert ───────────────────────────────────────────────────────────────────

def test_revert_is_noop():
    a = NotifySendActuator(binary="notify-send")
    # revert should not raise and should not call subprocess
    with patch("subprocess.run") as mock_run:
        ret = a.revert()
    assert ret is None
    mock_run.assert_not_called()
