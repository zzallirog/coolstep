"""coolstep dashboard — FastAPI backend.

Serves:
- /              → static index.html
- /api/health    → daemon liveness (does store.db exist? does ml-state.json exist?)
- /api/telemetry/latest → most recent frame from store
- /api/telemetry/range?since=15m → range query
- /api/calibration → gates state (computed in coolstep/core/calibration.py — Sprint I)
- /api/ml-state  → raw ml-state.json snapshot
- /api/adapters  → discovery probe (live: spawn collectors, return their cost)
- /api/stack-rationale → rendered ADR list from docs/stack-decisions.md
- /api/hot       → mmap hot-state pass-through (balance-plan step III)
- /api/self      → observer-effect self-monitor (balance-plan step II)

Frontend: Lit components. Tiles poll their endpoints at 1-30 Hz cadence —
no SSE (removed 2026-05-14, balance-plan step IV).
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import gc
import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

log = logging.getLogger(__name__)

import click
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from coolstep import __version__
from coolstep.adapters.actuators import discover as discover_actuators
from coolstep.adapters.collectors import discover as discover_collectors

# /api/adapters spawns every collector synchronously to measure cost. On
# Hyprland that's ~100 ms wall time per call. Tile refreshes every 30 s, so
# cache for slightly less than that to stay fresh without re-paying the cost.
_ADAPTERS_CACHE_SEC = 25.0
_adapters_cache: dict[str, object] = {"ts": 0.0, "body": None}
_collector_last_nonempty_at: dict[str, float] = {}

# /api/calibration runs 5 full-scan sqlite queries on frames table
# (118k+ rows): COUNT, MAX(cpu_temp), 2× filtered COUNT, COUNT DISTINCT.
# Each scan is ~100-200ms unconstrained, ~300-700ms under dashboard's
# CPU quota — call total ~1s wall. The gates state changes on the
# order of minutes (frame count / throttle events / workload labels
# evolve slowly), so a 30s cache shaves the per-poll cost without
# masking real state shifts.  Operator-flagged 2026-05-13: dashboard
# was hanging unevenly on cockpit polls — root cause was sibling tiles
# (calibration, discoveries) sharing the FastAPI event loop and
# blocking it for ~1s every poll cycle.
_CALIBRATION_CACHE_SEC = 30.0
_calibration_cache: dict[str, object] = {"ts": 0.0, "body": None}

# /api/discoveries rebuilds the signal manifest by re-instantiating every
# collector via discover_collectors() — ~300 ms unconstrained.  Signals
# don't change without a daemon restart, so a one-minute cache is more
# than safe.
_DISCOVERIES_CACHE_SEC = 60.0
_discoveries_cache: dict[str, object] = {"ts": 0.0, "body": None}

# Per-endpoint cache buckets. The 2-min browser-open simulation
# (operator-instrumented 2026-05-13) showed cockpit p99 = 7.7s under
# realistic 15-tile concurrent polling — root cause was sibling tiles
# (mode/profile/reliability/efficiency/drift/incidents/actuator-journal/
# predictor-breakdown/neighbours) running synchronous file IO or
# subprocesses on the FastAPI event loop. Each blocks the worker for
# ~100-1000 ms, queueing cockpit polls behind them. Caches + to_thread
# offload makes the worker non-blocking, which is the whole fix.
_GENERIC_CACHES: dict[str, dict[str, object]] = {}

# Per-route latency ring: 60 samples per route. Written by middleware on the
# event loop (single writer), read by /api/self-monitor (single reader).
# collections.deque is thread-safe for append+popleft, but we're single-loop
# so no lock is needed at all.
_ROUTE_LATENCY_RING: dict[str, collections.deque] = {}
_ROUTE_RING_SIZE = 60


def _cached_endpoint(key: str, ttl_sec: float):
    """Tiny helper to mirror the inline (_calibration_cache /
    _discoveries_cache / _adapters_cache) pattern without 9 copies of
    boilerplate. Returns (cached_body, put_callback) — None means miss.
    """
    now = time.monotonic()
    entry = _GENERIC_CACHES.get(key)
    if entry is not None and (now - float(entry["ts"])) < ttl_sec:
        return entry["body"], None

    def _put(body):  # noqa: ANN001
        _GENERIC_CACHES[key] = {"ts": time.monotonic(), "body": body}
        return body

    return None, _put


def _reset_endpoint_caches() -> None:
    """Test helper: drop every cached endpoint body so a fresh request
    re-runs the handler. Tests mutate the underlying files between
    calls and would otherwise see stale cached responses. Pytest
    conftest installs this as an autouse fixture in tests/dashboard/.
    """
    _GENERIC_CACHES.clear()
    _adapters_cache["ts"] = 0.0
    _adapters_cache["body"] = None
    _collector_last_nonempty_at.clear()
    _calibration_cache["ts"] = 0.0
    _calibration_cache["body"] = None
    _discoveries_cache["ts"] = 0.0
    _discoveries_cache["body"] = None
    _ROUTE_LATENCY_RING.clear()


def _percentile(data: list[float], pct: float) -> float:
    """Return the pct-th percentile (0-100) of sorted data. Caller must
    ensure len(data) >= 1."""
    idx = max(0, min(len(data) - 1, int(len(data) * pct / 100.0)))
    return data[idx]


def _coolstep_home() -> Path:
    return Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))


def _store_path() -> Path:
    return _coolstep_home() / "store.db"


def _ml_state_path() -> Path:
    return _coolstep_home() / "ml-state.json"


def _runtime_state_path() -> Path:
    """Daemon writes throttle FSM + armed_actions + mode here. Dashboard
    reads/writes the `mode` field only; everything else is owned by the
    daemon. Keeping the file shared (instead of routing through an HTTP
    or socket IPC) is the same convergence pattern as the rest of the
    system — the daemon polls it once per tick (~200B JSON)."""
    return _coolstep_home() / "runtime-state.json"


VALID_MODES = frozenset({"cool", "quiet", "off"})


def _stack_decisions_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "stack-decisions.md"


def _bench_runs_path() -> Path:
    """Path to bench/runs/ directory — at repo root, not under COOLSTEP_HOME."""
    return Path(__file__).resolve().parents[2] / "bench" / "runs"


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


def _residual_log_path() -> Path:
    return _coolstep_home() / "residual-state.jsonl"


def _residual_log():  # type: ignore[no-untyped-def]
    """Lazy import to avoid module-load failure if core/residual_log isn't
    on sys.path (e.g. minimal dashboard install without full daemon)."""
    try:
        from coolstep.core.residual_log import ResidualLog
        return ResidualLog(_residual_log_path())
    except Exception:  # noqa: BLE001
        return None


def _open_db():  # type: ignore[no-untyped-def]
    """Read-only sqlite handle to the frames store.  Used by the cockpit
    endpoint for past-30s temperature trail.  Best-effort: errors handled
    by caller."""
    import sqlite3
    path = _store_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    return conn


# Periodic gc.collect + malloc_trim(0). glibc holds freed chunks behind the
# sbrk high-water mark; Python pymalloc + C-extension allocs (uvloop,
# pydantic-core) don't return memory to the OS until trim() walks the top.
# Measured 2026-05-16 on this dashboard: RSS 302M → 207M (-95M / -31%) from
# one trim call after 25h uptime. MALLOC_TRIM_THRESHOLD_=131072 in env only
# affects automatic-on-free path; long-lived process needs an explicit nudge.
_MALLOC_TRIM_INTERVAL_SEC = 300.0


def _load_libc() -> ctypes.CDLL | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.malloc_trim.argtypes = [ctypes.c_size_t]
        libc.malloc_trim.restype = ctypes.c_int
        return libc
    except (OSError, AttributeError):
        return None


_LIBC = _load_libc()


def _trim_now() -> dict[str, object]:
    """Run gc + malloc_trim and report RSS delta. Used by /api/debug/trim and
    the periodic lifespan task. Idempotent, single-threaded, ~ms cost."""
    rss_before = _read_rss_kb()
    gc.collect()
    rc = -1
    if _LIBC is not None:
        rc = int(_LIBC.malloc_trim(0))
    rss_after = _read_rss_kb()
    return {
        "called": _LIBC is not None,
        "rc": rc,
        "rss_kb_before": rss_before,
        "rss_kb_after": rss_after,
        "rss_kb_freed": (
            rss_before - rss_after if rss_before and rss_after else None
        ),
    }


def _read_rss_kb() -> int | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


async def _periodic_malloc_trim() -> None:
    while True:
        try:
            await asyncio.sleep(_MALLOC_TRIM_INTERVAL_SEC)
            await asyncio.to_thread(_trim_now)
        except asyncio.CancelledError:
            return
        except Exception:
            # background hygiene must never crash the loop
            continue


def _warm_imports() -> None:
    """Pre-import hot modules so first handler invocation doesn't pay
    module-import cost synchronously on the event loop. Runs in a thread
    so import lock delays don't block startup."""
    try:
        from coolstep.core import calibration  # noqa: F401
    except Exception as exc:
        log.warning("pre-warm: calibration import failed: %s", exc)
    try:
        from coolstep.core import efficiency  # noqa: F401
    except Exception as exc:
        log.warning("pre-warm: efficiency import failed: %s", exc)
    try:
        from coolstep.core import drift  # noqa: F401
    except Exception as exc:
        log.warning("pre-warm: drift import failed: %s", exc)
    try:
        from coolstep.core import incidents  # noqa: F401
    except Exception as exc:
        log.warning("pre-warm: incidents import failed: %s", exc)
    try:
        from coolstep.core import residual_log  # noqa: F401
    except Exception as exc:
        log.warning("pre-warm: residual_log import failed: %s", exc)


def _warm_adapters_cache() -> None:
    """Pre-seed the adapters cache so first browser open is a cache hit.
    Failures are logged but must not propagate — pre-warm is best-effort."""
    try:
        collectors = discover_collectors()
        col_result = []
        for c in collectors:
            sample: dict = {}
            with suppress(Exception):
                sample = c.sample()
            if sample:
                _collector_last_nonempty_at[c.name] = time.time()
            cost = c.cost()
            col_result.append({
                "name": c.name,
                "discovered": True,
                "sample_us": cost.sample_us,
                "last_nonempty_at": _collector_last_nonempty_at.get(c.name),
                "rss_kb": cost.rss_kb,
                "signal_count": len(c.signals()) if hasattr(c, "signals") else 0,
            })
        from coolstep.adapters.actuators import discover as _disc_act
        from coolstep.core.schema import ActionVerb
        actuators = _disc_act()
        act_result = []
        for a in actuators:
            supported = [v.value for v in ActionVerb if a.supports(v)]
            act_result.append({
                "name": a.name,
                "discovered": True,
                "supports": supported,
            })
        body = {"collectors": col_result, "actuators": act_result}
        _adapters_cache["ts"] = time.monotonic()
        _adapters_cache["body"] = body
    except Exception as exc:
        log.warning("pre-warm adapters: %s", exc)


def _warm_discoveries_cache() -> None:
    """Pre-seed the discoveries cache."""
    try:
        from dataclasses import asdict
        collectors = discover_collectors()
        manifest: list[dict] = []
        for c in collectors:
            if not hasattr(c, "signals"):
                continue
            for sig in c.signals():
                row = asdict(sig)
                row["collector"] = c.name
                manifest.append(row)
        body: dict = {"signals": manifest, "total": len(manifest)}
        _discoveries_cache["ts"] = time.monotonic()
        _discoveries_cache["body"] = body
    except Exception as exc:
        log.warning("pre-warm discoveries: %s", exc)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    trim_task = asyncio.create_task(_periodic_malloc_trim())
    # Pre-warm: background tasks — failures must not block startup or lifespan.
    asyncio.create_task(asyncio.to_thread(_warm_imports))
    asyncio.create_task(asyncio.to_thread(_warm_adapters_cache))
    asyncio.create_task(asyncio.to_thread(_warm_discoveries_cache))
    try:
        yield
    finally:
        trim_task.cancel()
        with suppress(BaseException):
            await trim_task


def create_app(
    *,
    host_allowlist: frozenset[str] | None = None,
    origin_allowlist: frozenset[str] | None = None,
) -> FastAPI:
    from coolstep import __version__
    from coolstep.dashboard.security import (
        DEFAULT_HOST_ALLOWLIST,
        HardeningMiddleware,
        build_origin_allowlist,
    )

    app = FastAPI(
        title="coolstep dashboard", version=__version__, lifespan=_lifespan
    )
    app.add_middleware(
        HardeningMiddleware,
        host_allowlist=host_allowlist or DEFAULT_HOST_ALLOWLIST,
        origin_allowlist=origin_allowlist or build_origin_allowlist("127.0.0.1", 18889),
    )

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    class _LatencyMiddleware(BaseHTTPMiddleware):
        """Record per-route response time (ms) into a bounded ring of 60 samples.
        Written on the event loop — no lock needed (single-writer, deque append
        is atomic for CPython GIL). Reader is /api/self-monitor."""

        async def dispatch(self, request: StarletteRequest, call_next):  # type: ignore[override]
            t0 = time.perf_counter()
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            route = request.url.path
            ring = _ROUTE_LATENCY_RING.get(route)
            if ring is None:
                ring = collections.deque(maxlen=_ROUTE_RING_SIZE)
                _ROUTE_LATENCY_RING[route] = ring
            ring.append(elapsed_ms)
            return response

    app.add_middleware(_LatencyMiddleware)

    static_dir = _static_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        index_path = static_dir / "index.html"
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        cached, put = _cached_endpoint("health", 1.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        def _work() -> dict:
            store_p = _store_path()
            state_p = _ml_state_path()
            return {
                "daemon_seen": store_p.exists(),
                "store_path": str(store_p),
                "store_size_bytes": store_p.stat().st_size if store_p.exists() else 0,
                "ml_state_seen": state_p.exists(),
                "ml_state_age_sec": (
                    time.time() - state_p.stat().st_mtime if state_p.exists() else None
                ),
            }

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/telemetry/latest")
    async def telemetry_latest() -> JSONResponse:
        # 1s TTL: collector writes at ~1Hz; one shared cache covers 4 polling
        # tiles (live-telemetry + throttle-events + actuator-history + efficiency).
        cached, put = _cached_endpoint("telemetry_latest", 1.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        def _work() -> dict | None:
            path = _store_path()
            if not path.exists():
                return None
            conn = sqlite3.connect(path)
            try:
                row = conn.execute(
                    "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
                    "workload_label, raw_json FROM frames ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return None
            cols = ("ts", "cpu_temp", "cpu_power", "gpu_temp", "gpu_power",
                    "fan_max_rpm", "workload_label", "raw_json")
            out = dict(zip(cols, row, strict=True))
            if isinstance(out["raw_json"], str):
                with suppress(json.JSONDecodeError):
                    out["raw"] = json.loads(out["raw_json"])
            out.pop("raw_json", None)
            return out

        body = await asyncio.to_thread(_work)
        if body is None:
            path = _store_path()
            if not path.exists():
                return JSONResponse({"error": "no store yet"}, status_code=404)
            return JSONResponse({"error": "no frames yet"}, status_code=404)
        return JSONResponse(put(body))

    @app.get("/api/telemetry/range")
    async def telemetry_range(since: str = "15m", limit: int = 5000) -> JSONResponse:
        path = _store_path()
        if not path.exists():
            return JSONResponse({"frames": [], "error": "no store yet"})
        cutoff = time.time() - _parse_window(since)
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
                "workload_label FROM frames WHERE ts >= ? ORDER BY ts LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        finally:
            conn.close()
        cols = ("ts", "cpu_temp", "cpu_power", "gpu_temp", "gpu_power",
                "fan_max_rpm", "workload_label")
        return JSONResponse({"frames": [dict(zip(cols, r, strict=True)) for r in rows]})

    @app.get("/api/ml-state")
    async def ml_state() -> JSONResponse:
        cached, put = _cached_endpoint("ml_state", 1.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        def _work() -> dict | None:
            path = _ml_state_path()
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                return {"_error": str(exc)}

        body = await asyncio.to_thread(_work)
        if body is None:
            return JSONResponse({"error": "ml-state not written yet"}, status_code=404)
        if "_error" in body:
            return JSONResponse({"error": body["_error"]}, status_code=500)
        return JSONResponse(put(body))

    @app.get("/api/calibration")
    async def calibration() -> JSONResponse:
        now = time.monotonic()
        cached = _calibration_cache.get("body")
        if cached is not None and (now - float(_calibration_cache["ts"])) < _CALIBRATION_CACHE_SEC:
            return JSONResponse(cached)  # type: ignore[arg-type]
        from coolstep.core.calibration import evaluate

        # The 5 full-scan sqlite queries inside evaluate() are CPU-bound under
        # the dashboard's CPUQuota — running them on the FastAPI event loop
        # blocks every other endpoint for ~1s (cockpit polls queue up,
        # operator sees uneven hangs).  Offload to a worker thread so other
        # tiles keep responding while calibration recomputes.
        report = await asyncio.to_thread(evaluate, _store_path())
        body = report.to_dict()
        # frontend ожидает gates как dict — переформатируем под существующий dashboard.js
        gates_dict = {g["name"]: g for g in body["gates"]}  # type: ignore[index]
        body["gates"] = gates_dict
        _calibration_cache["ts"] = now
        _calibration_cache["body"] = body
        return JSONResponse(body)

    @app.get("/api/adapters")
    async def adapters() -> JSONResponse:
        now = time.monotonic()
        cached = _adapters_cache.get("body")
        if cached is not None and (now - float(_adapters_cache["ts"])) < _ADAPTERS_CACHE_SEC:
            return JSONResponse(cached)

        def _build_adapters() -> dict:
            from coolstep.core.schema import ActionVerb
            collectors = discover_collectors()
            col_result = []
            for c in collectors:
                sample: dict = {}
                with suppress(Exception):
                    sample = c.sample()
                if sample:
                    _collector_last_nonempty_at[c.name] = time.time()
                cost = c.cost()
                col_result.append({
                    "name": c.name,
                    "discovered": True,
                    "sample_us": cost.sample_us,
                    "last_nonempty_at": _collector_last_nonempty_at.get(c.name),
                    "rss_kb": cost.rss_kb,
                    "signal_count": len(c.signals()) if hasattr(c, "signals") else 0,
                })
            actuators = discover_actuators()
            act_result = []
            for a in actuators:
                supported = [v.value for v in ActionVerb if a.supports(v)]
                act_result.append({
                    "name": a.name,
                    "discovered": True,
                    "supports": supported,
                })
            return {"collectors": col_result, "actuators": act_result}

        body = await asyncio.to_thread(_build_adapters)
        _adapters_cache["ts"] = time.monotonic()
        _adapters_cache["body"] = body
        return JSONResponse(body)

    @app.get("/api/efficiency")
    async def efficiency(since: str = "7d") -> JSONResponse:
        # 2026-05-14 incident: TTL was 30s; under 307k frames in store the
        # `compute_historical` path runs json.loads on every row each miss
        # (efficiency.py:87, ~3M Python objects per call ≈ 700M heap peak).
        # GC didn't reclaim fast enough between back-to-back misses and the
        # process climbed to ~2.7G in two minutes. 300s TTL drops miss
        # frequency 10× without losing visible UX freshness — efficiency
        # curves over a 7d window evolve on a scale of hours, not seconds.
        # `gc.collect()` after the build returns the parsed-row temporaries
        # to the freelist immediately instead of waiting for generation-2
        # collection (which under MemoryHigh-throttle never gets to run).
        import gc
        from dataclasses import asdict

        cached, put = _cached_endpoint(f"efficiency:{since}", 300.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        from coolstep.core.efficiency import compute_historical

        def _work() -> dict:
            report = compute_historical(_store_path(), since_seconds=_parse_window(since))
            out = {
                "bins": [asdict(b) for b in report.bins],
                "sweet_spot_temp": report.sweet_spot_temp,
                "sweet_spot_efficiency": report.sweet_spot_efficiency,
                "knee_temp": report.knee_temp,
                "sample_count": report.sample_count,
                "t_ambient": report.t_ambient,
                "t_max": report.t_max,
            }
            gc.collect()
            return out

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/drift")
    async def drift() -> JSONResponse:
        cached, put = _cached_endpoint("drift", 5.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        from coolstep.core.drift import evaluate
        history_path = _coolstep_home() / "drift-history.jsonl"

        def _work() -> dict:
            return evaluate(_ml_state_path(), history_path).to_dict()

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/neighbours")
    async def neighbours() -> JSONResponse:
        """Latest predictor's top-K neighbours snapshot via ml-state.json."""
        cached, put = _cached_endpoint("neighbours", 1.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        path = _ml_state_path()
        if not path.exists():
            return JSONResponse({"neighbours": [], "model": None, "reason": "ml-state absent"})

        def _work() -> dict:
            try:
                state = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                return {"neighbours": [], "error": str(exc), "_status": 500}
            return {
                "neighbours": state.get("neighbours") or [],
                "model": state.get("model_name"),
                "reason": state.get("reason", ""),
                "throttle_prob": state.get("throttle_prob", 0.0),
                "confidence": state.get("confidence", 0.0),
                "chroma_count": state.get("chroma_count", 0),
                "embedder_fitted": state.get("embedder_fitted", False),
            }

        body = await asyncio.to_thread(_work)
        if body.get("_status") == 500:
            body.pop("_status", None)
            return JSONResponse(body, status_code=500)
        return JSONResponse(put(body))

    @app.get("/api/predictor-breakdown")
    async def predictor_breakdown() -> JSONResponse:
        """Surface enough of ml-state.json for the predictor-breakdown tile.

        Splits the final throttle_prob into its two contributing signals so
        the operator can see WHICH path drove a decision. Trajectory is
        re-computed server-side from features (mirrors
        ``predictor.py:KnnPredictor._trajectory_signal`` thresholds).
        """
        cached, put = _cached_endpoint("predictor_breakdown", 1.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        path = _ml_state_path()
        if not path.exists():
            return JSONResponse({"error": "no ml-state"}, status_code=404)

        def _work() -> dict:
            try:
                m = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                return {"error": str(exc), "_status": 500}

            features = m.get("features", {}) or {}
            cpu_temp_max = float(features.get("cpu_temp_max") or 0.0)
            cpu_temp_now = float(
                features.get("cpu_temp_now", features.get("cpu_temp_max")) or 0.0
            )
            slope = float(features.get("cpu_temp_slope_per_sec") or 0.0)
            cpu_load_max = float(features.get("cpu_load_max") or 0.0)

            HOT_NOW_C = 78.0
            FAST_SLOPE = 1.0
            MED_SLOPE = 0.5
            PAST_KNEE_C = 85.0
            trajectory_prob = 0.0
            if cpu_temp_now >= HOT_NOW_C and slope >= FAST_SLOPE:
                trajectory_prob = 0.9
            elif cpu_temp_now >= HOT_NOW_C and slope >= MED_SLOPE:
                trajectory_prob = 0.7
            elif cpu_temp_now >= PAST_KNEE_C:
                trajectory_prob = 0.65

            return {
                "model_name": m.get("model_name"),
                "throttle_prob": m.get("throttle_prob"),
                "confidence": m.get("confidence"),
                "expected_temp_c": m.get("expected_temp_c"),
                "reason": m.get("reason", ""),
                "features": {
                    "cpu_temp_now": cpu_temp_now,
                    "cpu_temp_max": cpu_temp_max,
                    "cpu_temp_slope_per_sec": slope,
                    "cpu_load_max": cpu_load_max,
                },
                "trajectory_prob_estimate": trajectory_prob,
            }

        body = await asyncio.to_thread(_work)
        if body.get("_status") == 500:
            body.pop("_status", None)
            return JSONResponse(body, status_code=500)
        return JSONResponse(put(body))

    @app.get("/api/predictor-cockpit")
    async def predictor_cockpit(scope_s: int = 30) -> JSONResponse:
        """Aggregated state for `<predictor-cockpit-tile>` — single round-trip
        instead of 3-4 polls.  Combines:
          - last 30s of (ts, actual_temp) from sqlite frames
          - last 30s of (ts, predicted_at_that_moment) from the residual log
          - current (t, dT/dt, predicted, σ, bucket, correction, base)
          - rolling accuracy (last 15 min median |residual|)
          - active tuned profile + last-flip ts

        See ADR-018.  This endpoint is read-only; all state lives in
        ml-state.json + residual-state.jsonl + sqlite frames.
        """
        import math as _math

        path = _ml_state_path()
        if not path.exists():
            return JSONResponse({"error": "no ml-state"}, status_code=404)

        scope_clamped = max(10, min(600, int(scope_s)))

        def _work() -> dict:  # noqa: PLR0912
            try:
                m = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                return {"_error": str(exc)}

            features = m.get("features", {}) or {}
            now = time.time()

            # Past 60s temperature trail from sqlite frames table.
            actual_trail: list[dict[str, float]] = []
            try:
                db = _open_db()
                since = now - 60.0
                rows = db.execute(
                    "SELECT ts, cpu_temp FROM frames "
                    "WHERE ts >= ? AND cpu_temp IS NOT NULL "
                    "ORDER BY ts ASC",
                    (since,),
                ).fetchall()
                db.close()
                actual_trail = [
                    {"ts_ago": round(now - row[0], 1), "t": float(row[1])}
                    for row in rows
                ]
            except Exception:  # noqa: BLE001
                actual_trail = []

            # Residual trail — binned by stable bucket key.
            residual_trail: list[dict[str, object]] = []
            try:
                rlog = _residual_log()
                if rlog is not None:
                    wide_tail = rlog.tail(max(600, scope_clamped * 5))
                    canvas_window = float(scope_clamped)
                    n_bins = 12
                    bin_width = max(0.5, canvas_window / max(1, n_bins))
                    picked: dict[int, object] = {}
                    for r in wide_tail:
                        age_validation = now - r.ts
                        if age_validation < 0 or age_validation > canvas_window:
                            continue
                        bucket_key = int(r.predicted_at // bin_width)
                        existing = picked.get(bucket_key)
                        if (
                            existing is None
                            or getattr(existing, "ts", 0) < r.ts  # type: ignore[arg-type]
                        ):
                            picked[bucket_key] = r
                    ordered = [picked[k] for k in sorted(picked.keys(), reverse=True)]
                    if not ordered:
                        ordered = list(reversed(wide_tail[-12:]))
                    for r in ordered[:n_bins]:
                        residual_trail.append({
                            "ts_ago": round(now - r.ts, 1),  # type: ignore[union-attr]
                            "predicted_at_ago": round(now - r.predicted_at, 1),  # type: ignore[union-attr]
                            "predicted": r.predicted_temp_c,  # type: ignore[union-attr]
                            "actual": r.actual_temp_c,  # type: ignore[union-attr]
                            "residual": round(r.residual_c, 2),  # type: ignore[union-attr]
                            "bucket_key": list(r.bucket_key) if r.bucket_key else None,  # type: ignore[union-attr]
                            "intervened": bool(getattr(r, "intervened", False)),
                            "intervention_verbs": list(getattr(r, "intervention_verbs", ())),
                        })
            except Exception:  # noqa: BLE001
                residual_trail = []

            # Accuracy: rolling 15-min median absolute residual.
            accuracy_pct: float | None = None
            median_abs_err: float | None = None
            passive_residual_count_15m = 0
            controlled_residual_count_15m = 0
            median_signed_err: float | None = None
            try:
                rlog2 = _residual_log()
                if rlog2 is not None:
                    wide = rlog2.tail(1024)
                    cutoff = now - 15 * 60
                    recent = [r for r in wide if r.ts >= cutoff]
                    if recent:
                        passive_recent = [r for r in recent if not getattr(r, "intervened", False)]
                        controlled_residual_count_15m = len(recent) - len(passive_recent)
                        passive_residual_count_15m = len(passive_recent)
                        accuracy_source = passive_recent or recent
                        abs_errs = sorted(abs(r.residual_c) for r in accuracy_source)
                        mid = abs_errs[len(abs_errs) // 2]
                        median_abs_err = round(mid, 2)
                        accuracy_pct = max(0.0, min(1.0, 1.0 - mid / 10.0))
                        signed = sorted(r.residual_c for r in accuracy_source)
                        median_signed_err = round(signed[len(signed) // 2], 2)
            except Exception:  # noqa: BLE001
                pass

            short_slope = features.get("cpu_temp_slope_per_sec_short")
            long_slope = features.get("cpu_temp_slope_per_sec")
            cur_t = float(features.get("cpu_temp_now", features.get("cpu_temp_max")) or 0.0)
            predicted = m.get("expected_temp_c")
            horizon = m.get("horizon_sec") or 30.0

            forecasts: dict[str, float] | None = None
            if predicted is not None and cur_t > 0:
                tau = 4.0
                horizon_f = float(horizon)
                f_h = 1.0 - _math.exp(-horizon_f / tau)
                if f_h > 1e-6:
                    full_delta = float(predicted) - cur_t

                    def _sample(h: float) -> float:
                        h_capped = min(h, horizon_f)
                        f = 1.0 - _math.exp(-h_capped / tau)
                        return round(cur_t + full_delta * (f / f_h), 2)

                    forecasts = {
                        "h5":  _sample(5.0),
                        "h15": _sample(15.0),
                        "h30": _sample(30.0),
                    }

            current = {
                "t": cur_t,
                "slope": float(
                    (short_slope if short_slope is not None else long_slope) or 0.0
                ),
                "accel": float(features.get("cpu_temp_accel_per_sec_sq") or 0.0),
                "load_slope": float(features.get("cpu_load_slope_per_sec") or 0.0),
                "predicted": predicted,
                "horizon_sec": horizon,
                "forecasts": forecasts,
                "confidence": m.get("confidence"),
                "model_name": m.get("model_name"),
                "reason": m.get("reason", ""),
                "ts": m.get("ts"),
                "age_sec": round(now - float(m.get("ts") or now), 1),
            }

            return {
                "current": current,
                "actual_trail": actual_trail,
                "residual_trail": residual_trail,
                "accuracy_pct": accuracy_pct,
                "median_abs_err_c": median_abs_err,
                "median_signed_err_c": median_signed_err,
                "passive_residual_count_15m": passive_residual_count_15m,
                "controlled_residual_count_15m": controlled_residual_count_15m,
                "active_tuned_profile": m.get("active_tuned_profile"),
                "profile_changed_at": m.get("profile_changed_at"),
                "meta_buckets": m.get("meta_buckets", 0),
                "residual_log_count": m.get("residual_log_count", 0),
                "spike": m.get("spike") or {"active": False},
                "prediction_age_sec": m.get("prediction_age_sec"),
                "predict_refresh_skipped": m.get("predict_refresh_skipped", 0),
                "predict_refresh_last_ms": m.get("predict_refresh_last_ms", 0.0),
                "predict_refresh_inflight": m.get("predict_refresh_inflight", False),
                "trust_mode": m.get("trust_mode", "prior"),
                "trust_n": m.get("trust_n", 0),
                "scope_s": scope_clamped,
            }

        result = await asyncio.to_thread(_work)
        if "_error" in result:
            return JSONResponse({"error": result["_error"]}, status_code=500)
        return JSONResponse(result)

    @app.get("/api/throttle-events")
    async def throttle_events(since: str = "7d", limit: int = 200) -> JSONResponse:
        cached, put = _cached_endpoint(f"throttle_events:{since}:{limit}", 5.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        def _work() -> dict:
            path = _store_path()
            if not path.exists():
                return {"events": [], "total": 0}
            cutoff = time.time() - _parse_window(since)
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute(
                    "SELECT ts_start, ts_end, duration, peak_temp, cause_label, "
                    "workload_at_start FROM throttle_events WHERE ts_start >= ? "
                    "ORDER BY ts_start DESC LIMIT ?",
                    (cutoff, limit),
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM throttle_events").fetchone()[0]
            finally:
                conn.close()
            cols = ("ts_start", "ts_end", "duration", "peak_temp",
                    "cause_label", "workload_at_start")
            events = [dict(zip(cols, r, strict=True)) for r in rows]
            return {"events": events, "total": int(total)}

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/discoveries")
    async def discoveries() -> JSONResponse:
        """Zabbix-LLD-style signal manifest aggregated over all collectors."""
        now = time.monotonic()
        cached = _discoveries_cache.get("body")
        if cached is not None and (now - float(_discoveries_cache["ts"])) < _DISCOVERIES_CACHE_SEC:
            return JSONResponse(cached)  # type: ignore[arg-type]
        from dataclasses import asdict

        def _build_manifest() -> dict[str, object]:
            collectors = discover_collectors()
            manifest: list[dict[str, object]] = []
            for c in collectors:
                if not hasattr(c, "signals"):
                    continue
                for sig in c.signals():
                    row = asdict(sig)
                    row["collector"] = c.name
                    manifest.append(row)
            return {"signals": manifest, "total": len(manifest)}

        # discover_collectors() probes /sys, /proc, runs subprocesses — never
        # block the event loop on it.  Same reasoning as the calibration
        # offload above; uvicorn's default executor handles the thread.
        body = await asyncio.to_thread(_build_manifest)
        _discoveries_cache["ts"] = now
        _discoveries_cache["body"] = body
        return JSONResponse(body)

    @app.get("/api/stack-rationale")
    async def stack_rationale() -> JSONResponse:
        path = _stack_decisions_path()
        if not path.exists():
            return JSONResponse({"adrs": []})
        return JSONResponse({"adrs": _parse_adrs(path.read_text(encoding="utf-8"))})

    @app.get("/api/actuator-journal")
    async def actuator_journal(limit: int = 50) -> JSONResponse:
        """Tail of `data/actuator-journal.jsonl`. Streams the file with a
        bounded deque so the worker doesn't read 575 KB into memory on every
        poll (same `deque(maxlen=n)` shape as `ResidualLog.tail`)."""
        if limit <= 0:
            limit = 50
        cached, put = _cached_endpoint(f"actuator_journal:{limit}", 5.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        path = _coolstep_home() / "actuator-journal.jsonl"

        def _work() -> dict:
            from collections import deque

            entries: list[dict[str, object]] = []
            if not path.exists():
                return {"entries": [], "total": 0}
            # 4× headroom for malformed lines we'll skip.
            keep = max(limit * 4, 200)
            try:
                with open(path, encoding="utf-8") as f:
                    tail_lines = deque(f, maxlen=keep)
            except OSError:
                return {"entries": [], "total": 0}
            for line in tail_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            entries.sort(
                key=lambda e: (e.get("ts") or e.get("applied_at") or 0.0),
                reverse=True,
            )
            return {"entries": entries[:limit], "total": len(entries)}

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/crash-recovery")
    async def crash_recovery() -> JSONResponse:
        """Return the last hard-crash recovery event written by coolstep-cleanup.sh,
        or ``{"recovered": False}`` if no recovery has ever occurred."""
        cached, put = _cached_endpoint("crash_recovery", 30.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        def _work() -> dict:
            path = _coolstep_home() / "last-crash-recovery.json"
            if not path.exists():
                return {"recovered": False}
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                return {"recovered": False}
            age_sec = time.time() - float(data.get("ts", 0))
            return {
                "recovered": True,
                "ts": data.get("ts"),
                "kind": data.get("kind"),
                "armed_count": data.get("armed_count", 0),
                "age_sec": age_sec,
            }

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/reliability")
    async def reliability() -> JSONResponse:
        """Daemon reliability metrics: uptime, restart count, last crash, MTBF.

        Sources:
          - subprocess ``systemctl --user show coolstep-collector.service``
            for ``ActiveEnterTimestampMonotonic`` + ``NRestarts``. Monotonic
            timestamps are µs since boot; uptime = now_mono - start_mono.
          - ``data/last-crash-recovery.json`` for the most recent hard-crash
            recovery event (same file as ``/api/crash-recovery``).
          - If no crashes have been recorded MTBF is reported as the current
            uptime (best case lower bound). If a crash exists, MTBF is the
            age of that crash (rough proxy — a real series would need history).

        Every branch tolerates missing/malformed inputs: keys stay ``None``
        rather than raising, so the tile always gets a parseable JSON body.
        """
        cached, put = _cached_endpoint("reliability", 30.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        import subprocess

        def _work() -> dict:
            body: dict[str, object] = {
                "uptime_sec": None,
                "restart_count": None,
                "last_crash": None,
                "mtbf_sec": None,
            }
            try:
                cp = subprocess.run(
                    ["systemctl", "--user", "show", "coolstep-collector.service",
                     "--property=ActiveEnterTimestampMonotonic,NRestarts"],
                    capture_output=True, timeout=2, check=False,
                )
                if cp.returncode == 0:
                    for line in cp.stdout.decode().splitlines():
                        if "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        if k == "ActiveEnterTimestampMonotonic" and v.isdigit():
                            try:
                                with open("/proc/uptime") as f:
                                    now_mono_sec = float(f.read().split()[0])
                                start_mono_sec = int(v) / 1_000_000.0
                                if int(v) > 0:
                                    body["uptime_sec"] = max(0.0, now_mono_sec - start_mono_sec)
                            except (OSError, ValueError):
                                pass
                        elif k == "NRestarts" and v.isdigit():
                            body["restart_count"] = int(v)
            except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
                pass

            crash_p = _coolstep_home() / "last-crash-recovery.json"
            if crash_p.exists():
                try:
                    crash_data = json.loads(crash_p.read_text())
                    ts = crash_data.get("ts")
                    body["last_crash"] = {
                        "ts": ts,
                        "kind": crash_data.get("kind"),
                        "age_sec": time.time() - float(ts) if ts is not None else None,
                    }
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass

            if body["last_crash"] is None and body["uptime_sec"] is not None:
                body["mtbf_sec"] = body["uptime_sec"]
            elif (
                body["last_crash"] is not None
                and isinstance(body["last_crash"], dict)
                and body["last_crash"].get("age_sec") is not None
            ):
                body["mtbf_sec"] = body["last_crash"]["age_sec"]
            return body

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/hot")
    async def hot_state() -> JSONResponse:
        """Balance-plan step III: live-telemetry from tmpfs mmap.

        Reads the daemon-published 64-byte struct at
        `$XDG_RUNTIME_DIR/coolstep/state.mmap`. Returns the same shape as
        a denormalised TelemetryFrame slice plus prediction overlays, all
        in flat scalars (None where the source was NaN). No cache — the
        underlying read is lock-free at ~microsecond cost; caching would
        only widen the freshness gap that this endpoint exists to close.

        404-style fallback: when the mmap file is absent (daemon not
        running yet, or running on a host without tmpfs) the body is
        `{"available": false}` and HTTP is still 200 — the frontend
        treats unavailable as a transient "wait" state, not an error.
        """
        from coolstep.core import hot_state as _hot

        def _work() -> dict:
            snap = _hot.read()
            if snap is None:
                return {"available": False}
            return {
                "available": True,
                "ts": snap.ts_sec,
                "age_sec": round(time.time() - snap.ts_sec, 3),
                "cpu_temp_now": snap.cpu_temp_now,
                "gpu_temp_max": snap.gpu_temp_max,
                "fan_max_rpm": snap.fan_max_rpm,
                "throttle_prob": snap.throttle_prob,
                "expected_temp_c": snap.expected_temp_c,
                "busy_ratio_ewma": snap.busy_ratio_ewma,
            }

        body = await asyncio.to_thread(_work)
        return JSONResponse(body)

    @app.get("/api/self")
    async def self_monitor() -> JSONResponse:
        """Balance-plan step II: observer-effect self-monitoring surface.

        Aggregates self-cost signals into a single body for `<self-monitor-tile>`:
          - `tick.*`            — from `ml-state.json:self_monitor` (busy_ratio, p95, slow_count)
          - `memory.*`          — cgroup memory.current / memory.swap.current / memory.max
          - `psi.*`             — cgroup cpu.pressure / memory.pressure / io.pressure (some.avg10)
          - `restarts.*`        — systemctl --user show NRestarts
          - `ml_state_age_sec`  — staleness of the ml-state.json file
          - `triggers`          — list of signal ids that flipped to TRIGGER (red dot)

        Cgroup paths are read once per request via `systemctl --user show
        ControlGroup`. Every probe is tolerant of EACCES/ENOENT/timeouts —
        missing values are `None`, never raise.
        """
        cached, put = _cached_endpoint("self_monitor", 1.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        import subprocess

        def _read_psi_some_avg10(path: Path) -> float | None:
            """Parse `some avg10=0.04 avg60=... avg300=... total=...` line."""
            try:
                for line in path.read_text().splitlines():
                    if line.startswith("some "):
                        for token in line.split():
                            if token.startswith("avg10="):
                                return float(token.split("=", 1)[1])
            except (OSError, ValueError, PermissionError):
                pass
            return None

        def _read_int(path: Path) -> int | None:
            try:
                v = path.read_text().strip()
                if v == "max":
                    return None
                return int(v)
            except (OSError, ValueError, PermissionError):
                pass
            return None

        def _cgroup_path(service: str) -> Path | None:
            """Resolve cgroup path via `systemctl --user show ControlGroup`."""
            try:
                cp = subprocess.run(
                    ["systemctl", "--user", "show", service, "--property=ControlGroup"],
                    capture_output=True, timeout=2, check=False,
                )
                if cp.returncode != 0:
                    return None
                for line in cp.stdout.decode().splitlines():
                    if line.startswith("ControlGroup="):
                        rel = line.split("=", 1)[1].strip()
                        if rel:
                            return Path("/sys/fs/cgroup") / rel.lstrip("/")
            except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
                pass
            return None

        def _restart_count(service: str) -> int | None:
            try:
                cp = subprocess.run(
                    ["systemctl", "--user", "show", service, "--property=NRestarts"],
                    capture_output=True, timeout=2, check=False,
                )
                if cp.returncode != 0:
                    return None
                for line in cp.stdout.decode().splitlines():
                    if line.startswith("NRestarts="):
                        v = line.split("=", 1)[1].strip()
                        if v.isdigit():
                            return int(v)
            except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
                pass
            return None

        # Thresholds: kept in-body so the frontend renders the bar correctly
        # without a second round-trip; also documents what each trigger means.
        THRESHOLDS = {
            "busy_ratio_p95": 1.0,           # tick exceeds period
            "slow_tick_count": 3,            # >=3 slow ticks in 60 = chronic
            "memory_used_pct": 80.0,         # cgroup memory.current vs max
            "memory_swap_mb": 1,             # >=1MB swap = thrash incoming
            "psi_some_avg10": 0.10,          # 10% pressure sustained
            "ml_state_age_sec": 5.0,         # UI gap user complained about (AGE 8s)
            "restart_count_delta": 1,        # any restart since last cache window
        }

        def _work() -> dict:
            body: dict[str, object] = {
                "ts": time.time(),
                "tick": None,
                "memory": {"current_mb": None, "swap_mb": None, "limit_mb": None, "used_pct": None},
                "psi": {"cpu_avg10": None, "memory_avg10": None, "io_avg10": None},
                "restarts": {"count": None},
                "ml_state_age_sec": None,
                "thresholds": THRESHOLDS,
                "triggers": [],
            }

            ml_state = _ml_state_path()
            if ml_state.exists():
                try:
                    body["ml_state_age_sec"] = round(time.time() - ml_state.stat().st_mtime, 2)
                    data = json.loads(ml_state.read_text())
                    sm = data.get("self_monitor")
                    if isinstance(sm, dict):
                        body["tick"] = sm
                except (OSError, json.JSONDecodeError):
                    pass

            cg = _cgroup_path("coolstep-collector.service")
            if cg is not None and cg.exists():
                mem_cur = _read_int(cg / "memory.current")
                mem_max = _read_int(cg / "memory.max")
                mem_swap = _read_int(cg / "memory.swap.current")
                body["memory"]["current_mb"] = (
                    round(mem_cur / 1024 / 1024, 1) if mem_cur is not None else None
                )
                body["memory"]["limit_mb"] = (
                    round(mem_max / 1024 / 1024, 1) if mem_max is not None else None
                )
                body["memory"]["swap_mb"] = (
                    round(mem_swap / 1024 / 1024, 1) if mem_swap is not None else None
                )
                if mem_cur is not None and mem_max is not None and mem_max > 0:
                    body["memory"]["used_pct"] = round(100.0 * mem_cur / mem_max, 1)
                body["psi"]["cpu_avg10"] = _read_psi_some_avg10(cg / "cpu.pressure")
                body["psi"]["memory_avg10"] = _read_psi_some_avg10(cg / "memory.pressure")
                body["psi"]["io_avg10"] = _read_psi_some_avg10(cg / "io.pressure")

            body["restarts"]["count"] = _restart_count("coolstep-collector.service")

            # Trigger evaluation. Each entry: {id, value, threshold, severity}.
            # Frontend renders one dot per known id from THRESHOLDS, painting
            # red if id is in triggers[], green otherwise.
            triggers: list[dict] = []
            tick = body.get("tick") if isinstance(body.get("tick"), dict) else None
            if isinstance(tick, dict):
                p95 = tick.get("busy_ratio_p95_60_ticks")
                if isinstance(p95, (int, float)) and p95 > THRESHOLDS["busy_ratio_p95"]:
                    triggers.append({"id": "busy_ratio_p95", "value": p95,
                                     "threshold": THRESHOLDS["busy_ratio_p95"]})
                slow = tick.get("slow_tick_count_60_ticks")
                if isinstance(slow, int) and slow >= THRESHOLDS["slow_tick_count"]:
                    triggers.append({"id": "slow_tick_count", "value": slow,
                                     "threshold": THRESHOLDS["slow_tick_count"]})
            pct = body["memory"].get("used_pct")
            if isinstance(pct, (int, float)) and pct > THRESHOLDS["memory_used_pct"]:
                triggers.append({"id": "memory_used_pct", "value": pct,
                                 "threshold": THRESHOLDS["memory_used_pct"]})
            swap_mb = body["memory"].get("swap_mb")
            if isinstance(swap_mb, (int, float)) and swap_mb >= THRESHOLDS["memory_swap_mb"]:
                triggers.append({"id": "memory_swap_mb", "value": swap_mb,
                                 "threshold": THRESHOLDS["memory_swap_mb"]})
            for axis in ("cpu_avg10", "memory_avg10", "io_avg10"):
                v = body["psi"].get(axis)
                if isinstance(v, (int, float)) and v > THRESHOLDS["psi_some_avg10"]:
                    triggers.append({"id": f"psi_{axis}", "value": v,
                                     "threshold": THRESHOLDS["psi_some_avg10"]})
            age = body.get("ml_state_age_sec")
            if isinstance(age, (int, float)) and age > THRESHOLDS["ml_state_age_sec"]:
                triggers.append({"id": "ml_state_age_sec", "value": age,
                                 "threshold": THRESHOLDS["ml_state_age_sec"]})
            body["triggers"] = triggers
            body["status"] = "trigger" if triggers else "ok"
            return body

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/stress-state")
    async def stress_state() -> JSONResponse:
        """Return the harness-published stress scenario state, or `{}` if absent/stale.

        Stale = (`started_at` + `duration_sec` + 600s) < now. File is opaque to
        the dashboard — its schema is owned by `bench/stress.sh` (PLAN §5).
        """
        cached, put = _cached_endpoint("stress_state", 10.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        def _work() -> dict:
            path = _coolstep_home() / "stress-state.json"
            if not path.exists():
                return {}
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                return {}
            if not isinstance(data, dict):
                return {}
            started = data.get("started_at")
            duration = data.get("duration_sec", 0) or 0
            if isinstance(started, (int, float)) and (
                float(started) + float(duration) + 600.0
            ) < time.time():
                return {}
            return data

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/stress-runs")
    async def stress_runs(limit: int = 20) -> JSONResponse:
        """Tail of bench/runs/index.json (written by bench/gc.py).

        Returns {"runs": [...], "count": N} or empty if file absent.
        bench/ lives in the repo root, not under COOLSTEP_HOME.
        """
        if limit <= 0:
            limit = 20
        index_p = _bench_runs_path() / "index.json"
        if not index_p.exists():
            return JSONResponse({"runs": [], "count": 0})
        try:
            data = json.loads(index_p.read_text())
        except (OSError, json.JSONDecodeError):
            return JSONResponse({"runs": [], "count": 0})
        runs = data.get("runs", [])[:limit]
        return JSONResponse({"runs": runs, "count": len(runs)})

    @app.get("/api/debug/heap")
    async def debug_heap(top: int = 25) -> JSONResponse:
        """Diagnostic only: tracemalloc top-N allocations grouped by line.

        Returns `{"enabled": false, ...}` when tracemalloc isn't active —
        enable by setting `PYTHONTRACEMALLOC=10` in the service env. The
        runtime cost of tracemalloc is non-trivial (~10-30% overhead per
        allocation) — leave it on only while diagnosing a heap incident.

        Why this endpoint exists: 2026-05-14 incident showed dashboard
        growing to ~2.7G in 2 min despite the SSE removal and MALLOC_ARENA_MAX=2
        mitigations. Python heap via pymalloc isn't visible to /proc/<pid>/smaps
        attribution. tracemalloc + this endpoint gives line-level numbers.
        """
        try:
            import tracemalloc  # stdlib
        except ImportError:
            return JSONResponse({"enabled": False, "reason": "tracemalloc unavailable"})
        if not tracemalloc.is_tracing():
            return JSONResponse({
                "enabled": False,
                "reason": "set PYTHONTRACEMALLOC=10 in service env and restart",
            })

        def _work() -> dict:
            snap = tracemalloc.take_snapshot()
            stats = snap.statistics("lineno")[:top]
            lines = []
            for s in stats:
                frame = s.traceback[0] if s.traceback else None
                lines.append({
                    "file": frame.filename if frame else "?",
                    "line": frame.lineno if frame else 0,
                    "size_kb": round(s.size / 1024, 1),
                    "count": s.count,
                })
            tracing_size, tracing_peak = tracemalloc.get_traced_memory()
            return {
                "enabled": True,
                "top": top,
                "traced_total_mb": round(tracing_size / 1024 / 1024, 1),
                "traced_peak_mb": round(tracing_peak / 1024 / 1024, 1),
                "lines": lines,
            }

        body = await asyncio.to_thread(_work)
        return JSONResponse(body)

    @app.get("/api/debug/trim")
    async def debug_trim() -> JSONResponse:
        """Manual trigger for gc + malloc_trim with before/after RSS.

        Same operation as the periodic lifespan task; exposed here so a
        verify run can quote concrete numbers. Safe to call any time."""
        body = await asyncio.to_thread(_trim_now)
        return JSONResponse(body)

    @app.get("/api/self-monitor")
    async def self_monitor_perf() -> JSONResponse:
        """Per-route latency ring (60 samples). Regression sentinel: if
        a future change drives any route's p95 above 200 ms, it shows here.

        Returns: {routes: {"/api/foo": {p50_ms, p95_ms, p99_ms, samples}}}
        """
        out: dict[str, dict[str, object]] = {}
        for route, ring in list(_ROUTE_LATENCY_RING.items()):
            samples = sorted(ring)
            n = len(samples)
            if n == 0:
                continue
            out[route] = {
                "p50_ms": round(_percentile(samples, 50), 2),
                "p95_ms": round(_percentile(samples, 95), 2),
                "p99_ms": round(_percentile(samples, 99), 2),
                "samples": n,
            }
        return JSONResponse({"routes": out})

    # Balance-plan step IV (2026-05-14): /api/sse/telemetry removed.
    # ADR-008 deprecated. The route was a `while True; yield; sleep(1)`
    # masquerade — not push semantics, just polling held open as a long-
    # lived connection. Long-lived starlette streams contributed to the
    # dashboard heap growth (anon 1.6G observed). Clients now poll
    # `/api/telemetry/latest` at 1 Hz directly.

    @app.get("/api/mode")
    async def get_mode() -> JSONResponse:
        """Return the current operational mode + safety context."""
        cached, put = _cached_endpoint("mode", 5.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        path = _runtime_state_path()
        ml = _ml_state_path()

        def _work() -> dict:
            body: dict[str, object] = {
                "mode": "cool",
                "valid_modes": sorted(VALID_MODES),
                "cpu_temp_c": None,
                "throttle_prob": None,
            }
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    stored = str(data.get("mode") or "cool").lower()
                    if stored in VALID_MODES:
                        body["mode"] = stored
                except (OSError, json.JSONDecodeError):
                    pass
            if ml.exists():
                try:
                    ml_data = json.loads(ml.read_text())
                    feats = ml_data.get("features", {}) or {}
                    body["cpu_temp_c"] = feats.get("cpu_temp_now", feats.get("cpu_temp_max"))
                    body["throttle_prob"] = ml_data.get("throttle_prob")
                except (OSError, json.JSONDecodeError):
                    pass
            return body

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    def _persist_mode(target: str, request: Request) -> JSONResponse:
        """Shared body for the per-action mode endpoints. Each action has its
        own URL (atrium-style: one verb per route) but they all funnel here
        because the persistence pattern is identical. Keeping the file-write
        in one place avoids three subtly-different copies drifting apart.

        Also appends the transition to `actuator-journal.jsonl` — the
        dashboard's mutating calls flow through the same audit trail as
        every other actuator event, so `/api/actuator-journal` and
        `coolstep inspect` already surface them with no extra plumbing."""
        from coolstep.adapters.actuators._base import append_journal_event

        path = _runtime_state_path()
        prev: str | None = None
        try:
            data: dict[str, object] = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if not isinstance(data, dict):
                        data = {}
                except json.JSONDecodeError:
                    data = {}
            prev_val = data.get("mode")
            if isinstance(prev_val, str):
                prev = prev_val
            data["mode"] = target
            data["mode_set_at"] = time.time()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        except OSError as exc:
            return JSONResponse(
                {"error": "persist failed", "detail": type(exc).__name__},
                status_code=500,
            )

        peer = request.client.host if request.client else None
        fwd = request.headers.get("x-forwarded-for")
        append_journal_event({
            "kind": "mode_switch",
            "actuator": "dashboard_api",
            "target": target,
            "from": prev,
            "peer": peer,
            "x_forwarded_for": fwd,
        })

        return JSONResponse({"mode": target, "applied_at": time.time()})

    @app.post("/api/mode/cool")
    async def set_mode_cool(request: Request) -> JSONResponse:
        """Anticipatory cooling — positive fan-curve bias on predicted peaks.
        Default operational mode; restores it after `quiet`/`off`."""
        return _persist_mode("cool", request)

    @app.post("/api/mode/quiet")
    async def set_mode_quiet(request: Request) -> JSONResponse:
        """Noise-reduction — subtractive fan-curve bias on confidently-calm
        windows. Safety belt evicts the bias automatically at Tctl >= 80°C."""
        return _persist_mode("quiet", request)

    @app.post("/api/mode/off")
    async def set_mode_off(request: Request) -> JSONResponse:
        """Observe-only — actuators stop firing actionable verbs; NOTIFY
        still flows so the user retains awareness of what *would* fire."""
        return _persist_mode("off", request)

    @app.get("/api/profile")
    async def get_profile() -> JSONResponse:
        """Active workload profile (CODE/RENDER/GAME/IDLE/OTHER)."""
        cached, put = _cached_endpoint("profile", 30.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        from coolstep.core.workload_profile import Profile, resolve_profile
        ml = _ml_state_path()

        def _work() -> dict:
            cls: str | None = None
            if ml.exists():
                try:
                    data = json.loads(ml.read_text())
                    cls = data.get("workload_label") or data.get("workload_class")
                except (OSError, json.JSONDecodeError):
                    pass
            profile = resolve_profile(cls).value if cls else Profile.OTHER.value
            return {"profile": profile, "workload_class": cls}

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/event-segments")
    async def get_event_segments(since: str = "24h", limit: int = 50) -> JSONResponse:
        """Recent session boundaries emitted by EventSegmenter.

        Today the segmenter lives in-memory on the daemon and doesn't
        persist boundaries — so this endpoint is a placeholder that
        returns an empty list. The dashboard tile tolerates the empty
        response. Once we wire a `data/segments.jsonl` (TODO), this
        endpoint will read it the same way `/api/incidents` reads
        `incidents.jsonl`."""
        return JSONResponse({"count": 0, "segments": []})

    @app.get("/api/efficiency-table")
    async def get_efficiency_table() -> JSONResponse:
        """Latest-per-bucket efficiency rows from data/efficiency_table.jsonl."""
        try:
            from coolstep.core.efficiency_calibration import load_table
        except ImportError:
            return JSONResponse({"rows": []})
        try:
            rows = load_table()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": type(exc).__name__, "rows": []})
        return JSONResponse({
            "count": len(rows),
            "rows": [
                {
                    "workload_class": r.workload_class,
                    "power_bucket_w": r.power_bucket_w,
                    "ambient_bucket_c": r.ambient_bucket_c,
                    "min_rpm_for_stable": r.min_rpm_for_stable,
                    "sample_count": r.sample_count,
                    "last_seen_ts": r.last_seen_ts,
                }
                for r in rows
            ],
        })

    @app.get("/api/incidents")
    async def get_incidents(since: str = "7d", limit: int = 50) -> JSONResponse:
        """List recent incidents (newest-first) within the trailing window."""
        cached, put = _cached_endpoint(f"incidents:{since}:{limit}", 15.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]
        from coolstep.core.incidents import read_incidents
        seconds_per_unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        floor: float | None = None
        try:
            if since and since[-1] in seconds_per_unit:
                n = float(since[:-1])
                floor = time.time() - n * seconds_per_unit[since[-1]]
        except (ValueError, IndexError):
            floor = None

        def _work() -> dict:
            rows = read_incidents(since_sec=floor, limit=limit)
            return {"count": len(rows), "since_sec": floor, "incidents": rows}

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/incidents/{ts:float}/similar")
    async def get_incident_similar(ts: float) -> JSONResponse:
        """Re-run the multi-angle similarity for a specific stored
        incident. Useful for the dashboard's expanded view where the
        user wants the *current* prior-set, not the snapshot stored at
        write time (which may be stale if more incidents accrued)."""
        from coolstep.core.incidents import find_similar, read_incidents
        rows = read_incidents(limit=1000)
        target = next((r for r in rows if abs(float(r.get("ts") or 0) - ts) < 1e-3), None)
        if target is None:
            return JSONResponse({"error": "incident not found"}, status_code=404)
        # Recompute against everything else on disk.
        prior = [r for r in rows if float(r.get("ts") or 0) != ts]
        matches = find_similar(target, prior=prior, top_k_per_angle=5)
        return JSONResponse({"ts": ts, "similar": matches})

    return app


def _latest_row() -> dict[str, object] | None:
    path = _store_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(path)
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute(
            "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
            "workload_label FROM frames ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cols = ("ts", "cpu_temp", "cpu_power", "gpu_temp", "gpu_power",
            "fan_max_rpm", "workload_label")
    return dict(zip(cols, row, strict=True))


def _parse_window(s: str) -> float:
    s = s.strip().lower()
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("d"):
        return float(s[:-1]) * 86400
    return float(s)


_ADR_HEADER_RE = re.compile(r"^## ADR-(\d{3}):\s*(.+?)$", re.MULTILINE)


def _parse_adrs(markdown: str) -> list[dict[str, str]]:
    """Split docs/stack-decisions.md into ADR entries.

    Returns: [{"id":"001","title":"...","body":"..."}].
    """
    matches = list(_ADR_HEADER_RE.finditer(markdown))
    out: list[dict[str, str]] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        out.append({
            "id": m.group(1),
            "title": m.group(2).strip(),
            "body": markdown[body_start:body_end].strip(),
        })
    return out


def _reload_factory() -> FastAPI:
    """Factory used by uvicorn --reload (re-imports on file changes).

    Reads bind config from env so the parent CLI can pass it through despite
    the factory being instantiated in a fresh subprocess each reload.
    """
    from coolstep.dashboard.security import (
        build_host_allowlist,
        build_origin_allowlist,
    )

    host = os.environ.get("COOLSTEP_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("COOLSTEP_DASH_PORT", "18889"))
    extras_raw = os.environ.get("COOLSTEP_DASH_ALLOW_HOST", "")
    extras = [h for h in extras_raw.split(",") if h.strip()]
    return create_app(
        host_allowlist=build_host_allowlist(host, extras=extras),
        origin_allowlist=build_origin_allowlist(host, port, extras=extras),
    )


@click.command()
@click.version_option(version=__version__, prog_name="coolstep-dashboard")
@click.option("--host", default="127.0.0.1",
              help="Bind interface. Loopback by default. Non-loopback requires --allow-public.")
@click.option("--port", default=18889, type=int)
@click.option("--reload", is_flag=True)
@click.option("--allow-public", is_flag=True,
              help="Required to bind to a non-loopback address. The dashboard "
                   "has NO authentication — see docs/security.md before using.")
@click.option("--allow-host", multiple=True, metavar="HOST",
              help="Additional Host: header value to accept (e.g. the FQDN of a "
                   "fronting reverse proxy). Repeatable.")
def run_dashboard(host: str, port: int, reload: bool,
                  allow_public: bool, allow_host: tuple[str, ...]) -> None:
    """Start the coolstep dashboard server."""
    import uvicorn

    from coolstep.dashboard.security import (
        LOOPBACK_HOSTS,
        build_host_allowlist,
        build_origin_allowlist,
    )

    is_loopback = host.lower() in LOOPBACK_HOSTS
    if not is_loopback and not allow_public:
        print(
            f"\nrefusing to bind {host}:{port} without --allow-public.\n\n"
            f"The dashboard has NO authentication. Anyone who can reach\n"
            f"this port can switch thermal modes on this machine.\n\n"
            f"Recommended deployments:\n"
            f"  1. Keep loopback bind (--host 127.0.0.1) and tunnel via SSH:\n"
            f"       ssh -L {port}:127.0.0.1:{port} user@host\n"
            f"  2. Front with an authenticating reverse proxy (Caddy / nginx +\n"
            f"     basic auth, Authelia, oauth2-proxy, etc.) then run:\n"
            f"       coolstep-dashboard --host {host} --port {port} \\\n"
            f"           --allow-public --allow-host <proxy-fqdn>\n\n"
            f"See docs/security.md and docs/headless-deployment.md.\n",
            file=sys.stderr,
        )
        sys.exit(2)

    if not is_loopback:
        print(
            f"WARNING: dashboard bound to {host}:{port} without authentication.\n"
            f"         Anyone reaching this port can switch thermal modes.\n"
            f"         Ensure an upstream auth proxy is enforcing access.\n"
            f"         See docs/security.md.\n",
            file=sys.stderr,
        )

    extras = list(allow_host)
    host_allowlist = build_host_allowlist(host, extras=extras)
    origin_allowlist = build_origin_allowlist(host, port, extras=extras)

    if reload:
        os.environ["COOLSTEP_DASH_HOST"] = host
        os.environ["COOLSTEP_DASH_PORT"] = str(port)
        os.environ["COOLSTEP_DASH_ALLOW_HOST"] = ",".join(extras)
        uvicorn.run("coolstep.dashboard.server:_reload_factory",
                    factory=True, host=host, port=port, reload=True)
    else:
        app = create_app(
            host_allowlist=host_allowlist,
            origin_allowlist=origin_allowlist,
        )
        uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    run_dashboard()
