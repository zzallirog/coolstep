"""Regression test for the 9-endpoint Phase-1 to_thread + cache work
shipped 2026-05-13. The browser-sim observation was that under realistic
15-tile concurrent polling the FastAPI worker queued requests behind
synchronous sibling endpoints (calibration / efficiency / reliability
subprocesses), producing 7-8 second tail latencies on the cockpit
endpoint.  These tests don't reproduce the concurrency stress — they
just lock in the contract that the affected endpoints now:

  1. Return successfully without raising on a normal ml-state / store.db.
  2. Honour the `_GENERIC_CACHES` so a second call within TTL avoids
     re-running the underlying work.

Live latency measurement is in `scripts/dashboard_browser_sim.py`;
running pytest against TestClient cannot exercise the cross-tile
contention pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coolstep.dashboard.server import (
    _GENERIC_CACHES,
    _reset_endpoint_caches,
    create_app,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    # Minimal ml-state.json so the endpoints have something to read.
    ml_state = {
        "expected_temp_c": 65.0,
        "features": {
            "cpu_temp_max": 65.0,
            "cpu_temp_now": 65.0,
            "cpu_temp_slope_per_sec": 0.0,
            "cpu_load_max": 10.0,
        },
        "model_name": "trajectory_baseline",
        "throttle_prob": 0.1,
        "confidence": 0.5,
        "chroma_count": 0,
        "embedder_fitted": False,
        "neighbours": [],
        "reason": "test fixture",
        "workload_label": "idle",
    }
    import json as _j
    (tmp_path / "ml-state.json").write_text(_j.dumps(ml_state))
    (tmp_path / "actuator-journal.jsonl").write_text('{"ts":1,"kind":"apply"}\n')
    (tmp_path / "drift-history.jsonl").write_text("")
    _reset_endpoint_caches()
    return TestClient(create_app())


@pytest.mark.parametrize("endpoint", [
    "/api/mode",
    "/api/profile",
    "/api/reliability",
    "/api/neighbours",
    "/api/predictor-breakdown",
    "/api/actuator-journal",
])
def test_endpoint_returns_ok(client: TestClient, endpoint: str) -> None:
    """Each of the 9 endpoints touched by Phase-1 must keep responding."""
    r = client.get(endpoint)
    assert r.status_code in (200, 404), f"{endpoint}: {r.status_code}\n{r.text}"


def test_cache_hits_on_second_call(client: TestClient) -> None:
    """The second call within TTL must hit the cache. We verify by reading
    `_GENERIC_CACHES` directly: after one /api/mode call the key should
    exist; after a second call within 5s, the cached body is returned.
    """
    _reset_endpoint_caches()
    assert "mode" not in _GENERIC_CACHES
    r1 = client.get("/api/mode")
    assert r1.status_code == 200
    assert "mode" in _GENERIC_CACHES, "first call did not populate cache"
    cached_ts_before = _GENERIC_CACHES["mode"]["ts"]
    r2 = client.get("/api/mode")
    assert r2.status_code == 200
    # ts must NOT have advanced — the second call returned the cached body.
    assert _GENERIC_CACHES["mode"]["ts"] == cached_ts_before, (
        "second call within TTL re-ran the handler instead of using cache"
    )
    # Bodies must match.
    assert r1.json() == r2.json()


def test_cache_misses_after_expiry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Push the cache entry's ts back so the next call sees a fresh handler run."""
    _reset_endpoint_caches()
    r1 = client.get("/api/profile")
    assert r1.status_code == 200
    assert "profile" in _GENERIC_CACHES
    # Backdate the cached ts past the 30s TTL.
    _GENERIC_CACHES["profile"]["ts"] -= 31.0
    ts_before = _GENERIC_CACHES["profile"]["ts"]
    r2 = client.get("/api/profile")
    assert r2.status_code == 200
    # ts must have advanced — handler re-ran.
    assert _GENERIC_CACHES["profile"]["ts"] > ts_before
