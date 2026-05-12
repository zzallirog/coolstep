"""ryzenadj_cap_boost actuator tests — all subprocess mocked."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import patch

from coolstep.adapters.actuators.ryzenadj_cap import (
    MIN_LIMIT_MW,
    RyzenadjCap,
    _parse_limits,
    make,
)
from coolstep.core.schema import Action, ActionVerb

# Sample ryzenadj -i output (representative subset)
SAMPLE_INFO_OUTPUT = """\
STAPM LIMIT          |    45.000 |    7.500 | slow-limit
PPT LIMIT FAST       |    65.000 |   12.345 | fast-limit
PPT LIMIT SLOW       |    45.000 |    9.876 | slow-limit
"""


def _completed(
    stdout: bytes = b"", returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["ryzenadj"], returncode=returncode, stdout=stdout, stderr=b"",
    )


def _action(intensity: float = 10.0, expires_in: float = 30.0) -> Action:
    return Action(
        verb=ActionVerb.CAP_BOOST,
        params={"intensity_pct": intensity},
        expires_at=time.time() + expires_in,
    )


# ── parser ────────────────────────────────────────────────────────────────


def test_parse_limits_extracts_values():
    limits = _parse_limits(SAMPLE_INFO_OUTPUT)
    assert limits is not None
    assert limits["stapm"] == 45_000
    assert limits["fast"] == 65_000
    assert limits["slow"] == 45_000


def test_parse_limits_returns_none_on_garbage():
    assert _parse_limits("") is None
    assert _parse_limits("some random output with no limit lines") is None
    # Partial — only STAPM present
    assert _parse_limits("STAPM LIMIT | 45.000 | 7.5 | slow-limit\n") is None


# ── discovery / make() ────────────────────────────────────────────────────


def test_make_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    with patch("shutil.which", return_value=None):
        assert make() is None


def test_make_returns_none_when_disabled_env(monkeypatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "false")
    with patch("shutil.which", return_value="/usr/bin/ryzenadj"):
        assert make() is None


def test_make_returns_actuator_when_supported(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    with patch("shutil.which", return_value="/usr/bin/ryzenadj"), \
         patch("subprocess.run", return_value=_completed(stdout=SAMPLE_INFO_OUTPUT.encode())):
        a = make()
    assert a is not None
    assert a.name == "ryzenadj_cap_boost"


# ── supports ─────────────────────────────────────────────────────────────


def test_supports_only_cap_boost():
    a = RyzenadjCap()
    assert a.supports(ActionVerb.CAP_BOOST) is True
    assert a.supports(ActionVerb.RAMP_COOLING) is False
    assert a.supports(ActionVerb.NOTIFY_USER) is False
    assert a.supports(ActionVerb.SHIFT_POWER_ENVELOPE) is False


# ── intensity clamping ────────────────────────────────────────────────────


def test_intensity_clamped():
    a = RyzenadjCap()
    # Upper clamp
    action_high = Action(
        verb=ActionVerb.CAP_BOOST,
        params={"intensity_pct": 100.0},
        expires_at=time.time() + 30.0,
    )
    assert a._intensity(action_high) == 20
    # Lower clamp
    action_low = Action(
        verb=ActionVerb.CAP_BOOST,
        params={"intensity_pct": 1.0},
        expires_at=time.time() + 30.0,
    )
    assert a._intensity(action_low) == 5


# ── biased limits floor ───────────────────────────────────────────────────


def test_biased_limits_floor_at_min():
    a = RyzenadjCap()
    # baseline 20W (20000 mW), intensity 20W → 0 mW → clamped to MIN_LIMIT_MW
    baseline = {"stapm": 20_000, "fast": 20_000, "slow": 20_000}
    result = a._biased_limits(baseline, intensity_w=20)
    assert result["stapm"] == MIN_LIMIT_MW
    assert result["fast"] == MIN_LIMIT_MW
    assert result["slow"] == MIN_LIMIT_MW


# ── apply dry-run ─────────────────────────────────────────────────────────


def test_apply_dryrun_does_not_call_write_subprocess(monkeypatch):
    """Default (env unset) = dry-run: only the -i baseline read fires, no write."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = RyzenadjCap()
    action = _action(intensity=10.0)

    write_calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        cmd_list = list(cmd)
        # Write path uses --stapm-limit / --fast-limit / --slow-limit
        if any("--stapm-limit" in s or "--fast-limit" in s for s in cmd_list):
            write_calls.append(cmd_list)
            raise AssertionError(f"write subprocess called in dry-run: {cmd_list}")
        # Baseline read: ryzenadj -i
        return _completed(stdout=SAMPLE_INFO_OUTPUT.encode())

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)

    assert result.error is None
    assert result.cmd_executed is not None
    assert "--fast-limit=" in result.cmd_executed
    assert result.stdout_tail.startswith("DRY-RUN")
    assert len(write_calls) == 0


# ── apply armed ───────────────────────────────────────────────────────────


def test_apply_armed_invokes_write(monkeypatch):
    """COOLSTEP_ACTUATOR_ENABLE=true → write subprocess fires with limit args."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = RyzenadjCap()
    action = _action(intensity=10.0)

    write_calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        cmd_list = list(cmd)
        if any("--stapm-limit" in s for s in cmd_list):
            write_calls.append(cmd_list)
            return _completed(stdout=b"ok")
        return _completed(stdout=SAMPLE_INFO_OUTPUT.encode())

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)

    assert result.error is None
    assert len(write_calls) == 1
    cmd = write_calls[0]
    assert any("--stapm-limit=" in s for s in cmd)
    assert any("--fast-limit=" in s for s in cmd)
    assert any("--slow-limit=" in s for s in cmd)
    # Limits should be reduced (65000 - 10000 = 55000 for fast)
    fast_arg = next(s for s in cmd if "--fast-limit=" in s)
    fast_val = int(fast_arg.split("=")[1])
    assert fast_val == 55_000


# ── revert ────────────────────────────────────────────────────────────────


def test_revert_re_applies_baseline_limits(monkeypatch):
    """Armed + after apply → revert re-issues baseline limits."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = RyzenadjCap()
    action = _action(intensity=10.0)

    def fake_run(cmd, **_kw):
        cmd_list = list(cmd)
        if any("--stapm-limit" in s for s in cmd_list):
            return _completed(stdout=b"ok")
        return _completed(stdout=SAMPLE_INFO_OUTPUT.encode())

    with patch("subprocess.run", side_effect=fake_run):
        a.apply(action)

    # Baseline now captured: stapm=45000, fast=65000, slow=45000
    revert_calls: list[list[str]] = []

    def fake_revert_run(cmd, **_kw):
        revert_calls.append(list(cmd))
        return _completed(stdout=b"ok")

    with patch("subprocess.run", side_effect=fake_revert_run):
        a.revert()

    assert len(revert_calls) == 1
    cmd = revert_calls[0]
    assert "--stapm-limit=45000" in cmd
    assert "--fast-limit=65000" in cmd
    assert "--slow-limit=45000" in cmd


# ── error handling ────────────────────────────────────────────────────────


def test_apply_handles_subprocess_failure(monkeypatch):
    """CalledProcessError during write → result.error contains 'returncode='."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = RyzenadjCap()
    action = _action(intensity=10.0)

    def fake_run(cmd, **_kw):
        cmd_list = list(cmd)
        if any("--stapm-limit" in s for s in cmd_list):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd_list, stderr=b"ryzenadj: permission denied",
            )
        return _completed(stdout=SAMPLE_INFO_OUTPUT.encode())

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)

    assert result.error is not None
    assert "returncode=" in result.error
