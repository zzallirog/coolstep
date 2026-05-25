"""Fixtures for perf budget tests.

Creates a temp COOLSTEP_HOME with:
- store.db populated with synthetic TelemetryFrames (realistic warm data)
- ml-state.json with a plausible predictor snapshot
- decisions.jsonl / incidents.jsonl / actuator-journal.jsonl stubs

Sets COOLSTEP_HOME env so all server._coolstep_home() calls resolve to tmp.

Frame count: 5 000 frames (~5 Hz × 16 min). 50k frames make suite slow
(~20s per write_frame due to commit-per-row); 5k gives a realistic
warmed-up store at sub-1s build cost and exercises the same sqlite paths.
"""

from __future__ import annotations

import json
import time

import pytest

from coolstep.core.schema import CpuMetrics, FanMetrics, GpuMetrics, TelemetryFrame
from coolstep.core.store import Store

_FRAME_COUNT = 5_000
_TICK_HZ = 5.0  # synthetic 5 Hz daemon cadence


def _make_frame(i: int, base_ts: float) -> TelemetryFrame:
    """Produce one synthetic frame. Temps oscillate gently to exercise
    calibration / drift / efficiency paths without triggering edge guards."""
    # 65–80 °C sawtooth over the batch
    cpu_temp = 65.0 + (i % 300) * 0.05
    return TelemetryFrame(
        timestamp=base_ts + i / _TICK_HZ,
        cpu=CpuMetrics(
            temps_c={"tctl": cpu_temp, "tccd1": cpu_temp - 2.0},
            power_w={"package": 15.0 + (i % 50) * 0.4},
            load_pct=[50.0 + (i % 20)] * 8,
        ),
        gpus=[GpuMetrics(name="amdgpu", temp_c=55.0 + (i % 30) * 0.3, power_w=8.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=2000 + (i % 10) * 50)],
    )


@pytest.fixture(scope="module")
def perf_home(tmp_path_factory):
    """Build a populated temp COOLSTEP_HOME; shared for the whole perf module."""
    home = tmp_path_factory.mktemp("perf_home")

    # Populate store.db with _FRAME_COUNT frames using batch insert for speed
    store = Store(home / "store.db")
    base_ts = time.time() - _FRAME_COUNT / _TICK_HZ
    # Batch via executemany — avoids per-row commit overhead
    frames_data = []
    for i in range(_FRAME_COUNT):
        ts = base_ts + i / _TICK_HZ
        cpu_temp = 65.0 + (i % 300) * 0.05
        frames_data.append((
            ts,
            cpu_temp,
            15.0 + (i % 50) * 0.4,  # cpu_power
            55.0 + (i % 30) * 0.3,  # gpu_temp
            8.0,                      # gpu_power
            2000 + (i % 10) * 50,    # fan_max_rpm
            "idle",                   # workload_label
            "{}",                     # raw_json
        ))
    store._conn.executemany(
        """
        INSERT OR REPLACE INTO frames
        (ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm,
         workload_label, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        frames_data,
    )
    store._conn.commit()

    # A handful of throttle events so throttle-events endpoint has data
    for j in range(5):
        ts_start = base_ts + j * 200.0
        store.write_throttle_event(
            ts_start=ts_start,
            ts_end=ts_start + 30.0,
            peak_temp=88.0,
            cause_label="sustained_high_temp",
            workload_at_start="idle",
        )
    store.close()

    # ml-state.json — minimal plausible predictor snapshot
    ml_state = {
        "ts": time.time() - 0.5,
        "model_name": "AlwaysIdleBaseline",
        "throttle_prob": 0.12,
        "confidence": 0.8,
        "expected_temp_c": 72.0,
        "horizon_sec": 30.0,
        "reason": "perf-fixture",
        "features": {
            "cpu_temp_now": 71.0,
            "cpu_temp_max": 73.0,
            "cpu_temp_slope_per_sec": 0.05,
            "cpu_temp_slope_per_sec_short": 0.03,
            "cpu_temp_accel_per_sec_sq": 0.001,
            "cpu_load_max": 60.0,
            "cpu_load_slope_per_sec": 0.1,
        },
        "active_tuned_profile": None,
        "profile_changed_at": None,
        "meta_buckets": 0,
        "residual_log_count": 0,
        "spike": {"active": False},
        "workload_label": "idle",
        "workload_class": "IDLE",
        "neighbours": [],
        "self_monitor": {
            "busy_ratio_p95_60_ticks": 0.3,
            "slow_tick_count_60_ticks": 0,
        },
    }
    (home / "ml-state.json").write_text(json.dumps(ml_state))

    # actuator-journal.jsonl — 10 synthetic entries
    journal_lines = []
    for k in range(10):
        journal_lines.append(json.dumps({
            "ts": time.time() - k * 60.0,
            "kind": "actuator_apply",
            "actuator": "readonly_log",
            "verb": "NOTIFY",
            "dry_run": False,
            "result": "ok",
        }))
    (home / "actuator-journal.jsonl").write_text("\n".join(journal_lines) + "\n")

    # incidents.jsonl — 3 synthetic incidents
    incidents_lines = []
    for m in range(3):
        incidents_lines.append(json.dumps({
            "ts": time.time() - m * 3600.0,
            "peak_temp": 87.0 + m,
            "duration": 45.0,
            "cause": "sustained_high_temp",
            "similar": [],
        }))
    (home / "incidents.jsonl").write_text("\n".join(incidents_lines) + "\n")

    # decisions.jsonl — 5 synthetic decisions
    decisions_lines = []
    for n in range(5):
        decisions_lines.append(json.dumps({
            "ts": time.time() - n * 120.0,
            "verb": "NOTIFY",
            "throttle_prob": 0.1 + n * 0.05,
            "dry_run": False,
        }))
    (home / "decisions.jsonl").write_text("\n".join(decisions_lines) + "\n")

    return home


@pytest.fixture(scope="module")
def perf_client(perf_home, monkeypatch_module):
    """TestClient wired to the populated perf_home."""
    monkeypatch_module.setenv("COOLSTEP_HOME", str(perf_home))
    from coolstep.dashboard import security
    from coolstep.dashboard.server import _reset_endpoint_caches, create_app

    augmented = security.DEFAULT_HOST_ALLOWLIST | {"testserver"}
    monkeypatch_module.setattr(security, "DEFAULT_HOST_ALLOWLIST", augmented)

    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as c:
        _reset_endpoint_caches()
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module(request):
    """Module-scoped monkeypatch (pytest only ships function-scoped by default)."""
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()
