"""Perf regression tests for server-cache-thread-prewarm changes.

Spec:
  1. /api/telemetry/latest cache hit returns in <10ms when warm.
  2. /api/adapters warm cache: 5 sequential hits finish in <50ms total.
  3. Lifespan startup does NOT increase by more than 1s — pre-warm tasks
     run as asyncio background tasks after yield, not blocking startup.
  4. /api/self-monitor route exists and returns per-route latency rings.
  5. New cache keys populated for health, throttle-events, ml-state.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coolstep.core.schema import CpuMetrics, FanMetrics, GpuMetrics, TelemetryFrame
from coolstep.core.store import Store
from coolstep.dashboard.server import (
    _GENERIC_CACHES,
    _ROUTE_LATENCY_RING,
    _reset_endpoint_caches,
    create_app,
)


@pytest.fixture
def client_with_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    home = tmp_path / "data"
    home.mkdir()
    store = Store(home / "store.db")
    frame = TelemetryFrame(
        timestamp=time.time(),
        cpu=CpuMetrics(temps_c={"tctl": 70.0}, power_w={"package": 25.0}),
        gpus=[GpuMetrics(name="g", temp_c=50.0, power_w=15.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=1800)],
    )
    store.write_frame(frame)
    store.close()
    monkeypatch.setenv("COOLSTEP_HOME", str(home))
    _reset_endpoint_caches()
    return TestClient(create_app())


def test_telemetry_latest_cache_hit_is_fast(
    client_with_store: TestClient,
) -> None:
    """Second call (cache-hit) must complete in under 10ms."""
    # Warm the cache.
    r1 = client_with_store.get("/api/telemetry/latest")
    assert r1.status_code == 200

    # Measure cache-hit latency.
    t0 = time.perf_counter()
    r2 = client_with_store.get("/api/telemetry/latest")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert r2.status_code == 200
    assert elapsed_ms < 10.0, (
        f"cache-hit took {elapsed_ms:.1f}ms (want <10ms); "
        "check _cached_endpoint TTL for telemetry_latest"
    )
    # Response body must be identical (same cache entry).
    assert r1.json()["ts"] == r2.json()["ts"]


def test_telemetry_latest_cache_key_has_no_per_client_params(
    client_with_store: TestClient,
) -> None:
    """Cache key must be route-level, not per-client. Two sequential
    calls within 1s must share the same cache entry."""
    _reset_endpoint_caches()
    client_with_store.get("/api/telemetry/latest")
    assert "telemetry_latest" in _GENERIC_CACHES
    ts_after_first = _GENERIC_CACHES["telemetry_latest"]["ts"]
    client_with_store.get("/api/telemetry/latest")
    assert _GENERIC_CACHES["telemetry_latest"]["ts"] == ts_after_first, (
        "second call updated cache ts — it bypassed the cache instead of hitting it"
    )


def test_adapters_warm_cache_is_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 warm /api/adapters hits must finish in <50ms total.

    The cold miss pays discover_collectors() cost (off event loop via to_thread).
    Warm hits should be pure dict serialisation — negligible.
    """
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("COOLSTEP_HOME", str(home))
    _reset_endpoint_caches()

    with TestClient(create_app()) as tc:
        # Cold miss — populates cache.
        r_cold = tc.get("/api/adapters")
        assert r_cold.status_code == 200

        # 5 warm hits — must be very fast (cache avoids all I/O).
        t0 = time.perf_counter()
        for _ in range(5):
            r = tc.get("/api/adapters")
            assert r.status_code == 200
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 50.0, (
        f"5 warm /api/adapters cache hits took {elapsed_ms:.1f}ms (want <50ms); "
        "cache may not be wired or TTL is 0"
    )


def test_lifespan_startup_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan startup must complete within 1s (pre-warm is async background,
    not blocking the yield transition). We measure time from create_app() call
    to first response, using TestClient's implicit lifespan."""
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("COOLSTEP_HOME", str(home))
    _reset_endpoint_caches()

    t0 = time.perf_counter()
    with TestClient(create_app()) as tc:
        r = tc.get("/api/health")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert r.status_code == 200
    assert elapsed_ms < 1000.0, (
        f"lifespan startup + first request took {elapsed_ms:.0f}ms (want <1000ms); "
        "pre-warm tasks may be blocking the yield transition"
    )


def test_self_monitor_endpoint_exists_and_returns_rings(
    client_with_store: TestClient,
) -> None:
    """After a few requests, /api/self-monitor must return non-empty rings."""
    # Generate some traffic so the ring has entries.
    for _ in range(3):
        client_with_store.get("/api/health")
    client_with_store.get("/api/telemetry/latest")

    r = client_with_store.get("/api/self-monitor")
    assert r.status_code == 200
    body = r.json()
    assert "routes" in body, f"expected 'routes' key: {body}"
    routes = body["routes"]
    assert len(routes) > 0, "latency ring is empty after several requests"

    # Each entry must have the required keys with valid values.
    for route, stats in routes.items():
        assert "p50_ms" in stats, f"{route} missing p50_ms"
        assert "p95_ms" in stats, f"{route} missing p95_ms"
        assert "p99_ms" in stats, f"{route} missing p99_ms"
        assert "samples" in stats, f"{route} missing samples"
        assert stats["samples"] > 0, f"{route} has 0 samples"
        assert stats["p50_ms"] >= 0.0
        assert stats["p95_ms"] >= stats["p50_ms"]
        assert stats["p99_ms"] >= stats["p95_ms"]


def test_self_monitor_ring_bounded_to_60_samples(
    client_with_store: TestClient,
) -> None:
    """Ring must not grow beyond _ROUTE_RING_SIZE (60)."""
    from coolstep.dashboard.server import _ROUTE_RING_SIZE

    for _ in range(80):
        client_with_store.get("/api/health")

    r = client_with_store.get("/api/self-monitor")
    assert r.status_code == 200
    body = r.json()

    # /api/health should be in the ring.
    stats = body["routes"].get("/api/health")
    assert stats is not None, "/api/health not in self-monitor ring"
    assert stats["samples"] <= _ROUTE_RING_SIZE, (
        f"ring grew to {stats['samples']} > {_ROUTE_RING_SIZE}"
    )


def test_throttle_events_cache_populated(
    client_with_store: TestClient,
) -> None:
    """/api/throttle-events must populate _GENERIC_CACHES within 5s TTL."""
    _reset_endpoint_caches()
    r = client_with_store.get("/api/throttle-events")
    assert r.status_code == 200

    cache_key = "throttle_events:7d:200"
    assert cache_key in _GENERIC_CACHES, (
        f"expected cache key {cache_key!r} not found in _GENERIC_CACHES after first call"
    )


def test_health_cache_populated(
    client_with_store: TestClient,
) -> None:
    """/api/health must populate _GENERIC_CACHES with 1s TTL."""
    _reset_endpoint_caches()
    r = client_with_store.get("/api/health")
    assert r.status_code == 200
    assert "health" in _GENERIC_CACHES


def test_ml_state_cache_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/api/ml-state must cache its response."""
    import json
    home = tmp_path / "data"
    home.mkdir()
    (home / "ml-state.json").write_text(json.dumps({"throttle_prob": 0.1}))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))
    _reset_endpoint_caches()
    tc = TestClient(create_app())

    r = tc.get("/api/ml-state")
    assert r.status_code == 200
    assert "ml_state" in _GENERIC_CACHES
