"""Tests for coolstep/inspect/doctor.py."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from coolstep.inspect.doctor import (
    CheckResult,
    check_journal_rotation_healthy,
    check_ml_state_fresh,
    check_runtime_state_no_stuck_armed,
    check_store_exists,
    check_systemd_session,
    run_all,
)


def _set_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# check_store_exists
# ---------------------------------------------------------------------------


def test_check_store_exists_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _set_home(monkeypatch, tmp_path)
    (home / "store.db").write_bytes(b"x" * 1024)
    result = check_store_exists()
    assert result.status == "ok"
    assert "store.db" in result.message


def test_check_store_exists_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_home(monkeypatch, tmp_path)
    result = check_store_exists()
    assert result.status == "fail"
    assert "not found" in result.message


# ---------------------------------------------------------------------------
# check_ml_state_fresh
# ---------------------------------------------------------------------------


def test_check_ml_state_fresh_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _set_home(monkeypatch, tmp_path)
    p = home / "ml-state.json"
    p.write_text("{}")
    # mtime is "now" by default — should be fresh
    result = check_ml_state_fresh()
    assert result.status == "ok"


def test_check_ml_state_fresh_warn_when_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    p = home / "ml-state.json"
    p.write_text("{}")
    stale_mtime = time.time() - 120
    os.utime(p, (stale_mtime, stale_mtime))
    result = check_ml_state_fresh()
    assert result.status == "warn"
    assert "stale" in result.message


# ---------------------------------------------------------------------------
# check_journal_rotation_healthy
# ---------------------------------------------------------------------------


def test_check_journal_rotation_warn_when_oversize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    p = home / "actuator-journal.jsonl"
    # Write ~2MB
    p.write_bytes(b"x" * (2 * 1024 * 1024))
    result = check_journal_rotation_healthy()
    assert result.status == "warn"
    assert "MB" in result.message


# ---------------------------------------------------------------------------
# check_systemd_session — headless probes (XDG, linger, /run/user/$UID/coolstep)
# ---------------------------------------------------------------------------


def test_check_systemd_session_warns_when_xdg_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    result = check_systemd_session()
    assert result.status == "warn"
    assert "XDG_RUNTIME_DIR unset" in result.message
    assert "loginctl enable-linger" in result.message


def test_check_systemd_session_warns_when_xdg_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))
    result = check_systemd_session()
    assert result.status == "warn"
    assert "directory missing" in result.message
    assert "loginctl enable-linger" in result.message


def test_check_systemd_session_warns_when_linger_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    mock_cp = MagicMock()
    mock_cp.stdout = b"Linger=no\n"
    with patch("coolstep.inspect.doctor.subprocess.run", return_value=mock_cp):
        result = check_systemd_session()
    assert result.status == "warn"
    assert "Linger=no" in result.message
    assert "loginctl enable-linger" in result.message


def test_check_systemd_session_ok_when_linger_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    mock_cp = MagicMock()
    mock_cp.stdout = b"Linger=yes\n"
    with patch("coolstep.inspect.doctor.subprocess.run", return_value=mock_cp):
        result = check_systemd_session()
    assert result.status == "ok"
    assert "writeable" in result.message
    # And the hot-state mmap path got created on disk
    assert (tmp_path / "coolstep").is_dir()


def test_check_systemd_session_warns_when_runtime_unwriteable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # Make hot-dir already exist but read-only — mkdir is fine,
    # the write_text probe should fail.
    (tmp_path / "coolstep").mkdir()
    (tmp_path / "coolstep").chmod(0o500)
    mock_cp = MagicMock()
    mock_cp.stdout = b"Linger=yes\n"
    try:
        with patch("coolstep.inspect.doctor.subprocess.run", return_value=mock_cp):
            result = check_systemd_session()
        assert result.status == "warn"
        assert "not writeable" in result.message
    finally:
        # restore mode so tmp_path can be cleaned
        (tmp_path / "coolstep").chmod(0o700)


# ---------------------------------------------------------------------------
# check_runtime_state_no_stuck_armed
# ---------------------------------------------------------------------------


def test_check_runtime_state_fail_with_stale_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    p = home / "runtime-state.json"
    stale_expires = time.time() - 3600  # 1h ago
    p.write_text(json.dumps({"armed_actions": [{"expires_at": stale_expires}]}))
    result = check_runtime_state_no_stuck_armed()
    assert result.status == "fail"
    assert "stale" in result.message


# ---------------------------------------------------------------------------
# run_all integration
# ---------------------------------------------------------------------------


def _patch_systemctl_and_http() -> list:
    """Return list of patch context managers for systemctl and urllib."""
    mock_cp = MagicMock()
    mock_cp.stdout = b"inactive"

    patcher_sub = patch(
        "coolstep.inspect.doctor.subprocess.run",
        return_value=mock_cp,
    )
    patcher_url = patch(
        "coolstep.inspect.doctor.urlopen",
        side_effect=URLError("connection refused"),
    )
    return [patcher_sub, patcher_url]


def test_run_all_returns_zero_when_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _set_home(monkeypatch, tmp_path)

    # Set up all file-based checks to pass
    (home / "store.db").write_bytes(b"x" * 512)

    ml_state = home / "ml-state.json"
    ml_state.write_text("{}")
    # mtime = now (fresh)

    # actuator armed = dry-run so baseline check skipped
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "dry-run")

    # Headless gate: XDG present + writeable; loginctl reports Linger=yes
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    def _fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        result = MagicMock()
        if cmd and cmd[0] == "loginctl":
            result.stdout = b"Linger=yes\n"
        else:
            result.stdout = b"active"
        return result

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(
        {"daemon_seen": True, "ml_state_age_sec": 5.0}
    ).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with (
        patch("coolstep.inspect.doctor.subprocess.run", side_effect=_fake_run),
        patch("coolstep.inspect.doctor.urlopen", return_value=mock_response),
    ):
        results, code = run_all()

    assert code == 0, [f"{r.name}={r.status}: {r.message}" for r in results if r.status != "ok"]


def test_run_all_returns_two_on_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _set_home(monkeypatch, tmp_path)
    # store.db absent → check_store_exists = fail
    # ml-state.json absent → check_ml_state_fresh = fail

    mock_cp = MagicMock()
    mock_cp.stdout = b"inactive"

    with (
        patch("coolstep.inspect.doctor.subprocess.run", return_value=mock_cp),
        patch("coolstep.inspect.doctor.urlopen", side_effect=URLError("refused")),
    ):
        results, code = run_all()

    assert code == 2
    names = {r.name for r in results if r.status == "fail"}
    assert "store_exists" in names
    assert "ml_state_fresh" in names
