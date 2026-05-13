"""coolstep dashboard — FastAPI + SSE backend.

Serves:
- /              → static index.html
- /api/health    → daemon liveness (does store.db exist? does ml-state.json exist?)
- /api/telemetry/latest → most recent frame from store
- /api/telemetry/range?since=15m → range query
- /api/calibration → gates state (computed in coolstep/core/calibration.py — Sprint I)
- /api/ml-state  → raw ml-state.json snapshot
- /api/adapters  → discovery probe (live: spawn collectors, return their cost)
- /api/stack-rationale → rendered ADR list from docs/stack-decisions.md
- /api/sse/telemetry → live stream (1 event per tick from poll loop)

Frontend (P0): plain HTML with fetch+SSE. Lit components — следующая сессия.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import click
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from coolstep.adapters.actuators import discover as discover_actuators
from coolstep.adapters.collectors import discover as discover_collectors

# /api/adapters spawns every collector synchronously to measure cost. On
# Hyprland that's ~100 ms wall time per call. Tile refreshes every 30 s, so
# cache for slightly less than that to stay fresh without re-paying the cost.
_ADAPTERS_CACHE_SEC = 25.0
_adapters_cache: dict[str, object] = {"ts": 0.0, "body": None}

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
    _calibration_cache["ts"] = 0.0
    _calibration_cache["body"] = None
    _discoveries_cache["ts"] = 0.0
    _discoveries_cache["body"] = None


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


def create_app() -> FastAPI:
    from coolstep import __version__

    app = FastAPI(title="coolstep dashboard", version=__version__)
    static_dir = _static_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        index_path = static_dir / "index.html"
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        store_p = _store_path()
        state_p = _ml_state_path()
        body = {
            "daemon_seen": store_p.exists(),
            "store_path": str(store_p),
            "store_size_bytes": store_p.stat().st_size if store_p.exists() else 0,
            "ml_state_seen": state_p.exists(),
            "ml_state_age_sec": (
                time.time() - state_p.stat().st_mtime if state_p.exists() else None
            ),
        }
        return JSONResponse(body)

    @app.get("/api/telemetry/latest")
    async def telemetry_latest() -> JSONResponse:
        path = _store_path()
        if not path.exists():
            return JSONResponse({"error": "no store yet"}, status_code=404)
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
                "workload_label, raw_json FROM frames ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return JSONResponse({"error": "no frames yet"}, status_code=404)
        cols = ("ts", "cpu_temp", "cpu_power", "gpu_temp", "gpu_power",
                "fan_max_rpm", "workload_label", "raw_json")
        body = dict(zip(cols, row, strict=True))
        if isinstance(body["raw_json"], str):
            try:
                body["raw"] = json.loads(body["raw_json"])
            except json.JSONDecodeError:
                pass
        body.pop("raw_json", None)
        return JSONResponse(body)

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
        path = _ml_state_path()
        if not path.exists():
            return JSONResponse({"error": "ml-state not written yet"}, status_code=404)
        try:
            return JSONResponse(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

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

        collectors = discover_collectors()
        col_result = []
        for c in collectors:
            try:
                c.sample()
            except Exception:  # noqa: BLE001
                pass
            cost = c.cost()
            col_result.append({
                "name": c.name,
                "discovered": True,
                "sample_us": cost.sample_us,
                "rss_kb": cost.rss_kb,
                "signal_count": len(c.signals()) if hasattr(c, "signals") else 0,
            })
        actuators = discover_actuators()
        act_result = []
        for a in actuators:
            from coolstep.core.schema import ActionVerb

            supported = [v.value for v in ActionVerb if a.supports(v)]
            act_result.append({
                "name": a.name,
                "discovered": True,
                "supports": supported,
            })
        body = {"collectors": col_result, "actuators": act_result}
        _adapters_cache["ts"] = now
        _adapters_cache["body"] = body
        return JSONResponse(body)

    @app.get("/api/efficiency")
    async def efficiency(since: str = "7d") -> JSONResponse:
        from dataclasses import asdict

        cached, put = _cached_endpoint(f"efficiency:{since}", 30.0)
        if cached is not None:
            return JSONResponse(cached)  # type: ignore[arg-type]

        from coolstep.core.efficiency import compute_historical

        def _work() -> dict:
            report = compute_historical(_store_path(), since_seconds=_parse_window(since))
            return {
                "bins": [asdict(b) for b in report.bins],
                "sweet_spot_temp": report.sweet_spot_temp,
                "sweet_spot_efficiency": report.sweet_spot_efficiency,
                "knee_temp": report.knee_temp,
                "sample_count": report.sample_count,
                "t_ambient": report.t_ambient,
                "t_max": report.t_max,
            }

        body = await asyncio.to_thread(_work)
        return JSONResponse(put(body))

    @app.get("/api/drift")
    async def drift() -> JSONResponse:
        cached, put = _cached_endpoint("drift", 30.0)
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

        v0.5.10: the entire body runs in `asyncio.to_thread` — earlier
        revisions did sync sqlite + tail() directly on the event loop,
        which serialised all other dashboard handlers behind the
        residual-log walk under load.  See P2.9.6 phase 2 rationale.
        """
        body, status = await asyncio.to_thread(_predictor_cockpit_sync, scope_s)
        return JSONResponse(body, status_code=status)

    def _predictor_cockpit_sync(scope_s: int) -> tuple[dict, int]:
        path = _ml_state_path()
        if not path.exists():
            return {"error": "no ml-state"}, 404
        try:
            m = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": str(exc)}, 500

        features = m.get("features", {}) or {}
        now = time.time()

        # Past 60s temperature trail from sqlite frames table. The
        # visible canvas window is ~38s (T_PAST = max(30, horizon+8));
        # the extra 20s+ buffer keeps the polyline anchored at the left
        # edge while the frontend's rAF pump slides X-positions left
        # between 2 s polls (P2.9.8, operator: «график едет», 2026-05-13).
        # Cost: ~300 floats (60s × 5Hz) per response, negligible.
        actual_trail: list[dict[str, float]] = []
        db = None
        try:
            db = _open_db()
            since = now - 60.0
            rows = db.execute(
                "SELECT ts, cpu_temp FROM frames "
                "WHERE ts >= ? AND cpu_temp IS NOT NULL "
                "ORDER BY ts ASC",
                (since,),
            ).fetchall()
            actual_trail = [
                {"ts_ago": round(now - row[0], 1), "t": float(row[1])}
                for row in rows
            ]
        except Exception:  # noqa: BLE001
            actual_trail = []
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

        # Residual trail — validated predictions across the canvas window
        # (2026-05-13: prior `tail(12)` packed 12 records from a 2.4s slice
        # since daemon validates @5Hz; every ring stacked at the −30s
        # canvas edge.  Now: walk a wider tail and bin by validation time
        # (`ts_ago`) — picks one freshest record per ~2.5s bucket over the
        # canvas T_PAST window so 12 rings span the full timeline). */
        residual_trail: list[dict[str, float | str | None]] = []
        try:
            log = _residual_log()
            if log is not None:
                # scope_s clamped to [10, 600] — operator-selected past
                # window for residual-trail distribution.  Default 30s
                # matches canvas T_PAST.  Larger scope walks further
                # back in the log so bin width grows accordingly.
                scope_clamped = max(10, min(600, int(scope_s)))
                wide_tail = log.tail(max(600, scope_clamped * 5))
                canvas_window = float(scope_clamped)
                n_bins = 12
                bin_width = max(0.5, canvas_window / max(1, n_bins))
                # Stable bucket key (2026-05-13 — operator: «цифры что
                # предиктилось плавают»).  Prior bucket = current-age /
                # bin_width changed each tick: a record at ts_ago=8s
                # belonged to bin 0; one tick later (ts_ago=10s) it
                # moved to bin 1, and bin 0's content rotated to a
                # fresher record — pin Y jumped because record changed.
                # New key: bucket index derived from absolute predicted_at
                # (rounded to bin_width).  Each record stays in the
                # same bucket forever; pins only update when a new
                # validation enters a fresh bucket or an old one ages
                # past the scope window.
                picked: dict[int, object] = {}
                for r in wide_tail:
                    age_validation = now - r.ts
                    if age_validation < 0 or age_validation > canvas_window:
                        continue
                    bucket_key = int(r.predicted_at // bin_width)
                    existing = picked.get(bucket_key)
                    # Keep youngest validation per stable bucket.
                    if (
                        existing is None
                        or getattr(existing, "ts", 0) < r.ts  # type: ignore[arg-type]
                    ):
                        picked[bucket_key] = r
                # Newest predicted_at bucket first.
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
                    })
        except Exception:  # noqa: BLE001
            residual_trail = []

        # Accuracy: rolling 15-min median absolute residual.
        accuracy_pct: float | None = None
        median_abs_err: float | None = None
        # Signed median (P2.9.2): consistently-negative residual = predictor
        # over-предсказывает, cooling выигрывает у historical envelope. Это
        # safe direction soft-cooling'а, рендерится зелёным.  Red reserved
        # для positive median (under-prediction, real warning).
        median_signed_err: float | None = None
        try:
            log = _residual_log()
            if log is not None:
                wide = log.tail(1024)
                cutoff = now - 15 * 60
                recent = [r for r in wide if r.ts >= cutoff]
                if recent:
                    abs_errs = sorted(abs(r.residual_c) for r in recent)
                    mid = abs_errs[len(abs_errs) // 2]
                    median_abs_err = round(mid, 2)
                    accuracy_pct = max(0.0, min(1.0, 1.0 - mid / 10.0))
                    signed = sorted(r.residual_c for r in recent)
                    median_signed_err = round(signed[len(signed) // 2], 2)
        except Exception:  # noqa: BLE001
            pass

        # Current snapshot for the cockpit center.  "LIVE NOW" is the
        # latest sample (cpu_temp_now); cpu_temp_max is a rolling-window
        # peak and is unsuitable as a live readout.  The dT/dt readout
        # prefers the short slope (~5 s) over the long one (~600 s) —
        # otherwise it averages over so much bidirectional jitter that it
        # reports ±0.001 °C/s and looks like a dead instrument.
        short_slope = features.get("cpu_temp_slope_per_sec_short")
        long_slope = features.get("cpu_temp_slope_per_sec")
        cur_t = float(features.get("cpu_temp_now", features.get("cpu_temp_max")) or 0.0)
        predicted = m.get("expected_temp_c")
        horizon = m.get("horizon_sec") or 30.0

        # Multi-horizon samples of the same meta-anchored forecast curve
        # (P2.9.3).  Curve formula (ADR-021): T(t) = T0 + (Tpred − T0)·F(t)/F(h),
        # F(t) = 1 − exp(−t/τ), τ=4s.  At t=h → Tpred exactly.
        #
        # Beyond-horizon clamp (regression fix 2026-05-13): when the model's
        # horizon_sec is shorter than a requested sample (e.g. always_idle
        # baseline runs at 5s, UI also offers 15s/30s tabs), extrapolating
        # F(t)/F(h) past h overshoots — at horizon=5s with cur=74, pred=56.5,
        # _sample(15) ≈ 50 < pred=56.5, breaking monotonicity. Cap requested
        # h at horizon_sec so any beyond-horizon tab honestly reports the
        # at-horizon prediction instead of inventing a number the model
        # didn't make.
        forecasts: dict[str, float] | None = None
        if predicted is not None and cur_t > 0:
            import math as _m
            tau = 4.0
            horizon_f = float(horizon)
            f_h = 1.0 - _m.exp(-horizon_f / tau)
            if f_h > 1e-6:
                full_delta = float(predicted) - cur_t
                def _sample(h: float) -> float:
                    h_capped = min(h, horizon_f)
                    f = 1.0 - _m.exp(-h_capped / tau)
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
            "active_tuned_profile": m.get("active_tuned_profile"),
            "profile_changed_at": m.get("profile_changed_at"),
            "meta_buckets": m.get("meta_buckets", 0),
            "residual_log_count": m.get("residual_log_count", 0),
            "spike": m.get("spike") or {"active": False},
            # Predictor refresh health surfacing (cockpit tile renders these
            # under the bucket-strip as "age N.Ns · skipped K · refresh Tms").
            "prediction_age_sec": m.get("prediction_age_sec"),
            "predict_refresh_skipped": m.get("predict_refresh_skipped", 0),
            "predict_refresh_last_ms": m.get("predict_refresh_last_ms", 0.0),
            "predict_refresh_inflight": m.get("predict_refresh_inflight", False),
            # P2.9.7 — meta-bucket trust regime (prior / shrunk / confident).
            "trust_mode": m.get("trust_mode", "prior"),
            "trust_n": m.get("trust_n", 0),
            "scope_s": max(10, min(600, int(scope_s))),
        }, 200

    @app.get("/api/throttle-events")
    async def throttle_events(since: str = "7d", limit: int = 200) -> JSONResponse:
        path = _store_path()
        if not path.exists():
            return JSONResponse({"events": [], "total": 0})
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
        return JSONResponse({"events": events, "total": int(total)})

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
        return JSONResponse({"adrs": _parse_adrs(path.read_text())})

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
        path = _coolstep_home() / "last-crash-recovery.json"
        if not path.exists():
            return JSONResponse({"recovered": False})
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return JSONResponse({"recovered": False})
        age_sec = time.time() - float(data.get("ts", 0))
        return JSONResponse({
            "recovered": True,
            "ts": data.get("ts"),
            "kind": data.get("kind"),
            "armed_count": data.get("armed_count", 0),
            "age_sec": age_sec,
        })

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

    @app.get("/api/stress-state")
    async def stress_state() -> JSONResponse:
        """Return the harness-published stress scenario state, or `{}` if absent/stale.

        Stale = (`started_at` + `duration_sec` + 600s) < now. File is opaque to
        the dashboard — its schema is owned by `bench/stress.sh` (PLAN §5).
        """
        path = _coolstep_home() / "stress-state.json"
        if not path.exists():
            return JSONResponse({})
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return JSONResponse({})
        if not isinstance(data, dict):
            return JSONResponse({})
        started = data.get("started_at")
        duration = data.get("duration_sec", 0) or 0
        if isinstance(started, (int, float)) and (
            float(started) + float(duration) + 600.0
        ) < time.time():
            return JSONResponse({})
        return JSONResponse(data)

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

    @app.get("/api/sse/telemetry")
    async def sse_telemetry(request: Request) -> StreamingResponse:
        async def gen():  # type: ignore[no-untyped-def]
            last_ts = 0.0
            while True:
                if await request.is_disconnected():
                    break
                row = _latest_row()
                if row and row["ts"] != last_ts:
                    last_ts = row["ts"]
                    yield f"data: {json.dumps(row)}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

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

    def _persist_mode(target: str) -> JSONResponse:
        """Shared body for the per-action mode endpoints. Each action has its
        own URL (atrium-style: one verb per route) but they all funnel here
        because the persistence pattern is identical. Keeping the file-write
        in one place avoids three subtly-different copies drifting apart."""
        path = _runtime_state_path()
        try:
            data: dict[str, object] = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if not isinstance(data, dict):
                        data = {}
                except json.JSONDecodeError:
                    data = {}
            data["mode"] = target
            data["mode_set_at"] = time.time()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: daemon polls this file every tick — a torn
            # `write_text` (open-truncate-write) interleaved with a read
            # raises JSONDecodeError and the daemon reverts the mode.
            # tmp + os.replace gives readers either the old or the new
            # bytes, never half of each.
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, path)
        except OSError as exc:
            return JSONResponse(
                {"error": "persist failed", "detail": type(exc).__name__},
                status_code=500,
            )
        return JSONResponse({"mode": target, "applied_at": time.time()})

    @app.post("/api/mode/cool")
    async def set_mode_cool() -> JSONResponse:
        """Anticipatory cooling — positive fan-curve bias on predicted peaks.
        Default operational mode; restores it after `quiet`/`off`."""
        return _persist_mode("cool")

    @app.post("/api/mode/quiet")
    async def set_mode_quiet() -> JSONResponse:
        """Noise-reduction — subtractive fan-curve bias on confidently-calm
        windows. Safety belt evicts the bias automatically at Tctl >= 80°C."""
        return _persist_mode("quiet")

    @app.post("/api/mode/off")
    async def set_mode_off() -> JSONResponse:
        """Observe-only — actuators stop firing actionable verbs; NOTIFY
        still flows so the user retains awareness of what *would* fire."""
        return _persist_mode("off")

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


@click.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=18889, type=int)
@click.option("--reload", is_flag=True)
def run_dashboard(host: str, port: int, reload: bool) -> None:
    """Start the coolstep dashboard server."""
    import uvicorn

    if reload:
        uvicorn.run("coolstep.dashboard.server:create_app", factory=True,
                    host=host, port=port, reload=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    run_dashboard()
