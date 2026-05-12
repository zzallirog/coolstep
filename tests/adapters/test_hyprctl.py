"""hyprctl collector tests with subprocess mocked."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from coolstep.adapters.collectors.hyprctl import HyprctlCollector, make


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["hyprctl"],
        returncode=returncode,
        stdout=stdout.encode(),
        stderr=b"",
    )


def _setup_live_sig(runtime_dir: Path, sig: str) -> Path:
    """Создаёт mocked Hyprland runtime: <runtime_dir>/<sig>/.socket.sock."""
    sig_dir = runtime_dir / sig
    sig_dir.mkdir(parents=True, exist_ok=True)
    sock = sig_dir / ".socket.sock"
    sock.touch()
    return sig_dir


def test_discover_false_without_env(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    c = HyprctlCollector()
    assert c.discover() is False


def test_discover_false_when_binary_missing(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    with patch("shutil.which", return_value=None):
        assert HyprctlCollector().discover() is False


def test_discover_true_when_env_and_binary(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    with patch("shutil.which", return_value="/usr/bin/hyprctl"), patch(
        "subprocess.run", return_value=_completed(stdout="Hyprland 0.40\n")
    ):
        assert HyprctlCollector().discover() is True


def test_sample_extracts_visible_workload(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    _setup_live_sig(tmp_path, "abcdef")
    clients = [
        {"class": "kitty", "pid": 1001, "mapped": True, "hidden": False},
        {"class": "firefox", "pid": 1002, "mapped": True, "hidden": False},
        {"class": "kitty", "pid": 1003, "mapped": True, "hidden": False},  # second kitty
        {"class": "obs-tray", "pid": 1004, "mapped": False, "hidden": True},
    ]
    fake_run = _completed(stdout=json.dumps(clients))

    c = HyprctlCollector(runtime_dir=tmp_path)
    with patch("subprocess.run", return_value=fake_run):
        partial = c.sample()
    workload = partial["workload"]
    assert workload is not None
    names = sorted(p.name for p in workload.top_processes)
    assert names == ["firefox", "kitty"]
    assert workload.rolling_features["visible_apps"] == 3.0
    assert workload.rolling_features["unique_classes"] == 2.0


def test_sample_handles_subprocess_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    _setup_live_sig(tmp_path, "abcdef")
    c = HyprctlCollector(runtime_dir=tmp_path)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="hyprctl", timeout=0.5)):
        partial = c.sample()
    assert partial == {}


def test_sample_handles_invalid_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    _setup_live_sig(tmp_path, "abcdef")
    c = HyprctlCollector(runtime_dir=tmp_path)
    with patch("subprocess.run", return_value=_completed(stdout="not json")):
        partial = c.sample()
    assert partial == {}


def test_sample_returns_empty_when_no_clients(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    _setup_live_sig(tmp_path, "abcdef")
    c = HyprctlCollector(runtime_dir=tmp_path)
    with patch("subprocess.run", return_value=_completed(stdout="[]")):
        partial = c.sample()
    workload = partial["workload"]
    assert workload is not None
    assert workload.top_processes == []
    assert workload.rolling_features["visible_apps"] == 0.0


def test_make_returns_collector_when_binary_present_even_without_env(monkeypatch):
    """Регистрация в registry должна пройти даже если env пока не пришёл.
    Это страховка от systemctl import-environment race на user-session start.
    """
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    with patch("shutil.which", return_value="/usr/bin/hyprctl"):
        collector = make()
    assert collector is not None
    assert collector.name == "hyprctl"


def test_make_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    with patch("shutil.which", return_value=None):
        assert make() is None


def test_sample_returns_empty_when_no_live_socket(monkeypatch, tmp_path):
    """Без живого socket в /run/user/<uid>/hypr/ sample молчит."""
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    c = HyprctlCollector(runtime_dir=tmp_path)  # empty
    with patch("subprocess.run") as mock_run:
        partial = c.sample()
    assert partial == {}
    assert mock_run.call_count == 0


def test_sample_self_recovers_when_socket_appears_later(monkeypatch, tmp_path):
    """Late env propagation: на первом sample socket'а нет, потом появляется."""
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    c = HyprctlCollector(runtime_dir=tmp_path)
    # 1-й tick — runtime dir пуст
    assert c.sample() == {}
    # появляется live socket
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    _setup_live_sig(tmp_path, "abcdef")
    clients = [{"class": "kitty", "pid": 1001, "mapped": True, "hidden": False}]
    with patch("subprocess.run", return_value=_completed(stdout=json.dumps(clients))):
        partial = c.sample()
    workload = partial["workload"]
    assert workload is not None
    assert workload.label == "kitty"


def test_sample_clears_cache_when_socket_disappears(monkeypatch, tmp_path):
    """Если socket уходит (Hyprland crashed/restart) — кеш сбрасывается."""
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abcdef")
    sig_dir = _setup_live_sig(tmp_path, "abcdef")
    c = HyprctlCollector(runtime_dir=tmp_path)
    clients = [{"class": "kitty", "pid": 1001, "mapped": True, "hidden": False}]
    with patch("subprocess.run", return_value=_completed(stdout=json.dumps(clients))):
        c.sample()
    assert c._cached_workload is not None
    # снести socket
    (sig_dir / ".socket.sock").unlink()
    sig_dir.rmdir()
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    assert c.sample() == {}
    assert c._cached_workload is None


def test_live_signature_falls_back_to_newest_runtime_dir(monkeypatch, tmp_path):
    """Если env-sig мёртва (нет socket'а) — берём newest dir с socket'ом из /run/user/<uid>/hypr/.

    Регрессия 2026-05-11: dashboard process с env от убитой Hyprland-сессии
    держал old signature. Теперь должен picknуть newest live.
    """
    # Симулируем три исторические сессии: старая (stale), средняя без socket, и newest
    _setup_live_sig(tmp_path, "old_sig")  # has socket but old mtime
    # Делаем "new_sig" более новой:
    import time as _time
    _time.sleep(0.01)
    new_sock = _setup_live_sig(tmp_path, "new_sig") / ".socket.sock"
    new_sock.touch()  # bump mtime
    # env указывает на мёртвую (не существующую в runtime dir) signature
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "stale_dead_sig")
    c = HyprctlCollector(runtime_dir=tmp_path)
    # _live_signature должен вернуть "new_sig", не "stale_dead_sig" и не "old_sig"
    assert c._live_signature() == "new_sig"


def test_live_signature_keeps_env_when_socket_exists(monkeypatch, tmp_path):
    """Если env-sig валидна (есть .socket.sock) — берём её, не сканируем."""
    _setup_live_sig(tmp_path, "env_sig")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "env_sig")
    c = HyprctlCollector(runtime_dir=tmp_path)
    assert c._live_signature() == "env_sig"


def test_sample_injects_live_sig_to_subprocess_env(monkeypatch, tmp_path):
    """Subprocess получает HYPRLAND_INSTANCE_SIGNATURE найденный через runtime probe,
    даже если в os.environ стоит другая (мёртвая) signature.
    """
    _setup_live_sig(tmp_path, "live_sig_xyz")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "stale_dead")  # не существует в tmp
    c = HyprctlCollector(runtime_dir=tmp_path)
    clients = [{"class": "kitty", "pid": 1001, "mapped": True, "hidden": False}]
    captured_env: dict[str, str] = {}

    def _capture(args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _completed(stdout=json.dumps(clients))

    with patch("subprocess.run", side_effect=_capture):
        partial = c.sample()
    assert partial.get("workload") is not None
    assert captured_env.get("HYPRLAND_INSTANCE_SIGNATURE") == "live_sig_xyz"
