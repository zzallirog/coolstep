"""Tests for `coolstep.dashboard.security` middleware + CLI bind guard.

These tests construct the app with an **explicit** host_allowlist that
does NOT include "testserver", overriding the conftest opt-in, so we can
assert the real production behaviour.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from coolstep.dashboard.security import (
    DEFAULT_HOST_ALLOWLIST,
    LOOPBACK_HOSTS,
    _host_from_header,
    build_host_allowlist,
    build_origin_allowlist,
)
from coolstep.dashboard.server import create_app


@pytest.fixture
def strict_client(tmp_path, monkeypatch):
    """Client with the production loopback-only host allowlist (no testserver)."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    app = create_app(
        host_allowlist=frozenset(DEFAULT_HOST_ALLOWLIST),
        origin_allowlist=build_origin_allowlist("127.0.0.1", 18889),
    )
    # base_url -> Host header = "127.0.0.1:18889" which IS in the allowlist.
    return TestClient(app, base_url="http://127.0.0.1:18889")


@pytest.fixture
def strict_app(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    return create_app(
        host_allowlist=frozenset(DEFAULT_HOST_ALLOWLIST),
        origin_allowlist=build_origin_allowlist("127.0.0.1", 18889),
    )


# ─── _host_from_header ─────────────────────────────────────────────────


def test_host_strip_port():
    assert _host_from_header("127.0.0.1:18889") == "127.0.0.1"


def test_host_lowercases():
    assert _host_from_header("LocalHost") == "localhost"


def test_host_ipv6_literal_with_port():
    assert _host_from_header("[::1]:18889") == "[::1]"


def test_host_none_or_empty():
    assert _host_from_header(None) is None
    assert _host_from_header("") is None


# ─── build_*_allowlist ─────────────────────────────────────────────────


def test_loopback_bind_expands_origin_to_all_loopback_names():
    origins = build_origin_allowlist("127.0.0.1", 18889)
    assert "http://127.0.0.1:18889" in origins
    assert "http://localhost:18889" in origins
    assert "http://[::1]:18889" in origins


def test_host_allowlist_includes_extras():
    out = build_host_allowlist("0.0.0.0", extras=["coolstep.example.net"])
    assert "coolstep.example.net" in out
    assert "0.0.0.0" in out
    assert "127.0.0.1" in out  # loopback baseline preserved


# ─── Host header validation (DNS rebinding) ───────────────────────────


def test_host_loopback_accepted(strict_client):
    r = strict_client.get("/api/health")
    assert r.status_code == 200


def test_host_attacker_rebind_rejected(strict_app):
    """An attacker-controlled hostname resolving to 127.0.0.1 must 421."""
    client = TestClient(strict_app, base_url="http://attacker.example.com")
    r = client.get("/api/health")
    assert r.status_code == 421
    body = r.json()
    assert body["error"] == "host_not_allowed"
    assert "security.md" in body["detail"]


def test_host_missing_rejected(strict_app):
    """Requests with no Host header at all are rejected — HTTP/1.1 requires one."""
    client = TestClient(strict_app, base_url="http://attacker.example.com")
    r = client.get("/api/health", headers={"host": ""})
    assert r.status_code == 421


# ─── Form-CSRF defense on mutating methods ────────────────────────────


def test_post_same_origin_allowed(strict_client):
    """Browser POST with Sec-Fetch-Site: same-origin gets through."""
    r = strict_client.post(
        "/api/mode/cool",
        headers={
            "origin": "http://127.0.0.1:18889",
            "sec-fetch-site": "same-origin",
        },
    )
    assert r.status_code == 200


def test_post_cross_site_form_blocked(strict_client):
    """A form POST from evil.com must be rejected even though no CORS headers exist."""
    r = strict_client.post(
        "/api/mode/quiet",
        headers={
            "origin": "https://evil.example.com",
            "sec-fetch-site": "cross-site",
        },
    )
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "csrf_blocked"


def test_post_cross_origin_xhr_blocked(strict_client):
    """Even without Sec-Fetch-Site, off-allowlist Origin alone is enough to block."""
    r = strict_client.post(
        "/api/mode/quiet",
        headers={"origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_post_curl_no_browser_headers_allowed(strict_client):
    """curl-style POST (no Origin, no Sec-Fetch-Site) is allowed — attacker
    scenario for form-CSRF requires a browser context which sends at least one."""
    r = strict_client.post("/api/mode/off")
    assert r.status_code == 200


def test_get_unaffected_by_csrf_check(strict_client):
    """GETs are not gated by Origin/Sec-Fetch; only mutating methods are."""
    r = strict_client.get(
        "/api/health",
        headers={"origin": "https://evil.example.com"},
    )
    assert r.status_code == 200


# ─── CLI bind guard ───────────────────────────────────────────────────


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Invoke run_dashboard via -c to exercise its click entrypoint."""
    code = (
        "from coolstep.dashboard.server import run_dashboard; "
        f"run_dashboard.main({list(args)!r}, standalone_mode=False)"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_cli_refuses_public_bind_without_flag():
    """`--host 0.0.0.0` without `--allow-public` must exit non-zero."""
    proc = _run_cli("--host", "0.0.0.0", "--port", "0")
    assert proc.returncode != 0
    assert "allow-public" in proc.stderr.lower()
    assert "security.md" in proc.stderr.lower()


def test_cli_loopback_bind_does_not_require_flag(monkeypatch):
    """Loopback bind should not trip the guard. We use `--port 0` and patch
    uvicorn.run to a no-op so the test exits immediately after the guard."""
    code = (
        "import sys; "
        "sys.modules.setdefault('uvicorn', __import__('types').ModuleType('uvicorn')); "
        "sys.modules['uvicorn'].run = lambda *a, **k: None; "
        "from coolstep.dashboard.server import run_dashboard; "
        "run_dashboard.main(['--host', '127.0.0.1', '--port', '0'], standalone_mode=False)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "refusing to bind" not in proc.stderr


def test_loopback_hosts_constant():
    """Regression: localhost variants must stay in the loopback set."""
    assert "127.0.0.1" in LOOPBACK_HOSTS
    assert "localhost" in LOOPBACK_HOSTS
    assert "::1" in LOOPBACK_HOSTS


# ─── Mode-switch audit journal ────────────────────────────────────────


def test_mode_switch_appends_to_actuator_journal(strict_client, tmp_path):
    """POST /api/mode/* must leave a record in actuator-journal.jsonl so the
    existing /api/actuator-journal endpoint and inspect CLI surface it."""
    import json

    r = strict_client.post("/api/mode/quiet")
    assert r.status_code == 200

    journal = tmp_path / "actuator-journal.jsonl"
    assert journal.exists(), "journal file should have been created"
    lines = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    mode_events = [e for e in lines if e.get("kind") == "mode_switch"]
    assert mode_events, f"expected mode_switch event, got {lines}"
    last = mode_events[-1]
    assert last["target"] == "quiet"
    assert last["actuator"] == "dashboard_api"
    assert "ts" in last


def test_mode_switch_records_previous_mode(strict_client, tmp_path):
    """Transition `from` field captures the prior mode for transition graphs."""
    import json

    strict_client.post("/api/mode/cool")
    strict_client.post("/api/mode/quiet")

    journal = tmp_path / "actuator-journal.jsonl"
    events = [
        json.loads(line) for line in journal.read_text().splitlines()
        if line.strip() and json.loads(line).get("kind") == "mode_switch"
    ]
    assert len(events) >= 2
    assert events[-1]["target"] == "quiet"
    assert events[-1]["from"] == "cool"
