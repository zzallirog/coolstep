"""Dashboard FastAPI route tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    TelemetryFrame,
)
from coolstep.core.store import Store
from coolstep.dashboard.server import _parse_adrs, _parse_window, create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    store = Store(home / "store.db")
    frame = TelemetryFrame(
        timestamp=1000.0,
        cpu=CpuMetrics(temps_c={"tctl": 75.0}, power_w={"package": 30.0}),
        gpus=[GpuMetrics(name="g", temp_c=55.0, power_w=20.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=2000)],
    )
    store.write_frame(frame)
    store.close()
    monkeypatch.setenv("COOLSTEP_HOME", str(home))
    app = create_app()
    return TestClient(app)


def test_health_when_store_exists(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["daemon_seen"] is True
    assert body["store_size_bytes"] > 0


def test_telemetry_latest(client):
    r = client.get("/api/telemetry/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["ts"] == 1000.0
    assert body["cpu_temp"] == 75.0
    assert body["gpu_temp"] == 55.0


def test_telemetry_range(client):
    r = client.get("/api/telemetry/range?since=1d")
    assert r.status_code == 200
    body = r.json()
    assert "frames" in body


def test_calibration_initial_state(client):
    r = client.get("/api/calibration")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert "coverage_hours" in body["gates"]


def test_adapters_returns_list(client):
    r = client.get("/api/adapters")
    assert r.status_code == 200
    body = r.json()
    assert "collectors" in body


def test_stack_rationale_parses(client):
    r = client.get("/api/stack-rationale")
    assert r.status_code == 200
    body = r.json()
    assert len(body["adrs"]) >= 5  # docs/stack-decisions.md содержит 12 ADR


def test_ml_state_404_when_absent(client):
    r = client.get("/api/ml-state")
    assert r.status_code == 404


def test_index_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "coolstep" in r.text.lower()


def test_efficiency_route(client):
    r = client.get("/api/efficiency?since=1h")
    assert r.status_code == 200
    body = r.json()
    assert "bins" in body
    assert "t_ambient" in body


def test_drift_route(client):
    r = client.get("/api/drift")
    assert r.status_code == 200
    body = r.json()
    assert "severity" in body
    assert "indicators" in body


def test_neighbours_route_works_without_state(client):
    r = client.get("/api/neighbours")
    assert r.status_code == 200
    body = r.json()
    assert "neighbours" in body
    assert isinstance(body["neighbours"], list)


def test_throttle_events_returns_empty_initially(client):
    r = client.get("/api/throttle-events?since=1d")
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert isinstance(body["events"], list)
    assert "total" in body


def test_adapters_returns_collectors_and_actuators(client):
    r = client.get("/api/adapters")
    assert r.status_code == 200
    body = r.json()
    assert "collectors" in body
    assert "actuators" in body
    if body["actuators"]:
        a = body["actuators"][0]
        assert "name" in a
        assert "supports" in a
        assert isinstance(a["supports"], list)


def test_discoveries_returns_signal_manifest(client):
    r = client.get("/api/discoveries")
    assert r.status_code == 200
    body = r.json()
    assert "signals" in body
    assert isinstance(body["signals"], list)
    assert body["total"] >= 0
    if body["total"] > 0:
        sample = body["signals"][0]
        assert "name" in sample
        assert "unit" in sample
        assert "collector" in sample


def test_actuator_journal_empty_when_no_apply_calls(client):
    r = client.get("/api/actuator-journal")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert isinstance(body["entries"], list)
    assert "total" in body
    assert isinstance(body["total"], int)


def test_actuator_journal_returns_entries_after_apply(client, monkeypatch):
    """After populating an actuator journal, route should surface the entries."""
    import time as _time

    from coolstep.adapters.actuators.readonly import ReadonlyActuator
    from coolstep.core.schema import Action, ActionVerb

    fake = ReadonlyActuator()
    fake.apply(Action(verb=ActionVerb.RAMP_COOLING,
                      params={"intensity_pct": 10.0},
                      expires_at=_time.time() + 30.0))
    fake.apply(Action(verb=ActionVerb.NOTIFY_USER,
                      params={"msg": "test"},
                      expires_at=_time.time() + 10.0))

    monkeypatch.setattr(
        "coolstep.dashboard.server.discover_actuators",
        lambda: [fake],
    )
    r = client.get("/api/actuator-journal?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["entries"]) == 2
    # Sorted by applied_at desc → most recent first
    assert body["entries"][0]["applied_at"] >= body["entries"][1]["applied_at"]
    e = body["entries"][0]
    assert e["actuator"] == "readonly_log"
    assert "stdout_tail" in e
    assert "cmd_executed" in e
    assert "error" in e
    assert "expires_at" in e  # key always present even if None


def test_actuator_journal_limit_clamps_correctly(client, monkeypatch):
    from coolstep.adapters.actuators.readonly import ReadonlyActuator
    from coolstep.core.schema import Action, ActionVerb

    fake = ReadonlyActuator()
    for _ in range(5):
        fake.apply(Action(verb=ActionVerb.NOTIFY_USER, params={}, expires_at=0.0))
    monkeypatch.setattr(
        "coolstep.dashboard.server.discover_actuators",
        lambda: [fake],
    )
    r = client.get("/api/actuator-journal?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2
    assert body["total"] == 5


def test_stress_state_returns_empty_when_file_absent(client):
    r = client.get("/api/stress-state")
    assert r.status_code == 200
    assert r.json() == {}


def test_stress_state_returns_payload_when_recent(client, tmp_path, monkeypatch):
    import json as _json
    import time as _time

    home = tmp_path / "stress-data"
    home.mkdir()
    payload = {
        "scenario": "S1",
        "armed": True,
        "started_at": _time.time(),
        "duration_sec": 120,
        "pid": 99999,
    }
    (home / "stress-state.json").write_text(_json.dumps(payload))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    # Re-create the app with the new COOLSTEP_HOME
    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/stress-state")
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "S1"
    assert body["armed"] is True
    assert body["duration_sec"] == 120


def test_stress_state_treats_stale_file_as_empty(client, tmp_path, monkeypatch):
    import json as _json

    home = tmp_path / "stress-stale"
    home.mkdir()
    payload = {
        "scenario": "S2",
        "started_at": 1000.0,  # Unix epoch +0 — definitely stale
        "duration_sec": 30,
    }
    (home / "stress-state.json").write_text(_json.dumps(payload))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/stress-state")
    assert r.status_code == 200
    assert r.json() == {}


def test_stress_state_handles_malformed_json(client, tmp_path, monkeypatch):
    home = tmp_path / "stress-malformed"
    home.mkdir()
    (home / "stress-state.json").write_text("{not json")
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/stress-state")
    assert r.status_code == 200
    assert r.json() == {}


def test_crash_recovery_empty_when_file_absent(client):
    r = client.get("/api/crash-recovery")
    assert r.status_code == 200
    assert r.json() == {"recovered": False}


def test_crash_recovery_populated(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    home = tmp_path / "crash-data"
    home.mkdir()
    (home / "last-crash-recovery.json").write_text(_json.dumps({
        "ts": _time.time() - 60.0,
        "kind": "baseline_restore",
        "armed_count": 2,
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/crash-recovery")
    assert r.status_code == 200
    body = r.json()
    assert body["recovered"] is True
    assert body["kind"] == "baseline_restore"
    assert body["armed_count"] == 2
    assert 50.0 < body["age_sec"] < 120.0


def test_parse_window_units():
    assert _parse_window("60s") == 60.0
    assert _parse_window("5m") == 300.0
    assert _parse_window("2h") == 7200.0
    assert _parse_window("1d") == 86400.0
    assert _parse_window("90") == 90.0


def test_parse_adrs_extracts_id_and_title():
    md = (
        "# Stack decisions\n\n"
        "## ADR-001: First decision\n"
        "Body of first.\n\n"
        "## ADR-002: Second decision\n"
        "Body of second.\n"
    )
    adrs = _parse_adrs(md)
    assert len(adrs) == 2
    assert adrs[0]["id"] == "001"
    assert adrs[0]["title"] == "First decision"
    assert "Body of first" in adrs[0]["body"]


def test_stress_runs_empty_when_no_index(client):
    r = client.get("/api/stress-runs")
    assert r.status_code == 200
    assert r.json() == {"runs": [], "count": 0}


def test_stress_runs_returns_index_runs(client, tmp_path, monkeypatch):
    """index.json present → API surfaces its runs."""
    import json as _json

    bench_dir = tmp_path / "bench_runs"
    bench_dir.mkdir()
    payload = {
        "rebuilt_at": 1778535999.12,
        "count": 2,
        "runs": [
            {
                "dir": "20260512T010101-S1",
                "scenario": "S1",
                "armed": True,
                "duration_sec": 120,
                "started_at": 1778535000.0,
                "peak_tctl": 93.5,
                "time_above_85": 18,
                "first_predict_age": 4.5,
                "first_rampup_age": 17.3,
                "delta_sec": 12.8,
                "verdict": "coolstep_moved_first",
            },
            {
                "dir": "20260512T020202-S2",
                "scenario": "S2",
                "armed": False,
                "duration_sec": 60,
                "started_at": 1778536000.0,
                "peak_tctl": 88.0,
                "time_above_85": 5,
                "first_predict_age": None,
                "first_rampup_age": None,
                "delta_sec": None,
                "verdict": "not_observed",
            },
        ],
    }
    (bench_dir / "index.json").write_text(_json.dumps(payload))
    monkeypatch.setattr("coolstep.dashboard.server._bench_runs_path", lambda: bench_dir)

    r = client.get("/api/stress-runs")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert len(body["runs"]) == 2
    assert body["runs"][0]["scenario"] == "S1"
    assert body["runs"][0]["verdict"] == "coolstep_moved_first"
    assert body["runs"][1]["scenario"] == "S2"
    assert body["runs"][1]["verdict"] == "not_observed"


def test_stress_runs_limit_clamps(client, tmp_path, monkeypatch):
    """limit param caps returned runs."""
    import json as _json

    bench_dir = tmp_path / "bench_runs_limit"
    bench_dir.mkdir()
    runs = [
        {"dir": f"run-{i}", "scenario": "S1", "verdict": "not_observed"}
        for i in range(5)
    ]
    (bench_dir / "index.json").write_text(_json.dumps({"runs": runs, "count": 5}))
    monkeypatch.setattr("coolstep.dashboard.server._bench_runs_path", lambda: bench_dir)

    r = client.get("/api/stress-runs?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert len(body["runs"]) == 2


def test_predictor_breakdown_404_when_no_ml_state(client):
    r = client.get("/api/predictor-breakdown")
    assert r.status_code == 404


def test_predictor_breakdown_returns_breakdown_when_traj_dominates(tmp_path, monkeypatch):
    """ml-state with hot cpu + steep slope → trajectory_prob_estimate=0.9."""
    import json as _json

    home = tmp_path / "pb-data"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "tick": 100, "ts": 1.0,
        "model_name": "knn_v1",
        "throttle_prob": 0.9, "confidence": 0.7,
        "expected_temp_c": 95.0,
        "reason": "0/20 neighbours hot | trajectory: 82.0°C rising 1.50°C/s",
        "features": {
            "cpu_temp_max": 82.0,
            "cpu_temp_slope_per_sec": 1.5,
            "cpu_load_max": 95.0,
        },
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/predictor-breakdown")
    assert r.status_code == 200
    body = r.json()
    assert body["model_name"] == "knn_v1"
    assert body["throttle_prob"] == 0.9
    assert body["confidence"] == 0.7
    assert body["expected_temp_c"] == 95.0
    assert body["features"]["cpu_temp_max"] == 82.0
    assert body["features"]["cpu_temp_slope_per_sec"] == 1.5
    assert body["features"]["cpu_load_max"] == 95.0
    assert body["trajectory_prob_estimate"] == 0.9
    assert "trajectory" in body["reason"]


def test_predictor_breakdown_med_slope_band(tmp_path, monkeypatch):
    """78°C + slope 0.5..1.0 → trajectory_prob_estimate=0.7."""
    import json as _json

    home = tmp_path / "pb-data-med"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "knn_v1",
        "throttle_prob": 0.4, "confidence": 0.5,
        "expected_temp_c": 84.0,
        "reason": "warming",
        "features": {
            "cpu_temp_max": 79.0,
            "cpu_temp_slope_per_sec": 0.6,
            "cpu_load_max": 88.0,
        },
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/predictor-breakdown")
    assert r.status_code == 200
    assert r.json()["trajectory_prob_estimate"] == 0.7


def test_predictor_breakdown_past_knee(tmp_path, monkeypatch):
    """Past-knee (>=85°C) with no slope info → trajectory_prob_estimate=0.65."""
    import json as _json

    home = tmp_path / "pb-data-knee"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "knn_v1",
        "throttle_prob": 0.65, "confidence": 0.5,
        "expected_temp_c": 90.0,
        "reason": "trajectory: 86.0°C (already past knee)",
        "features": {
            "cpu_temp_max": 86.0,
            "cpu_temp_slope_per_sec": 0.0,
            "cpu_load_max": 70.0,
        },
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/predictor-breakdown")
    assert r.status_code == 200
    assert r.json()["trajectory_prob_estimate"] == 0.65


def test_predictor_breakdown_cool_returns_zero_trajectory(tmp_path, monkeypatch):
    """Under all thresholds → trajectory_prob_estimate=0 (KNN owns the signal)."""
    import json as _json

    home = tmp_path / "pb-data-cool"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "knn_v1",
        "throttle_prob": 0.25, "confidence": 0.6,
        "expected_temp_c": 72.0,
        "reason": "5/20 neighbours hot in 30s (coverage 100%, agreement 75%)",
        "features": {
            "cpu_temp_max": 70.0,
            "cpu_temp_slope_per_sec": 0.2,
            "cpu_load_max": 50.0,
        },
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/predictor-breakdown")
    assert r.status_code == 200
    assert r.json()["trajectory_prob_estimate"] == 0.0


def test_predictor_breakdown_handles_malformed_json(tmp_path, monkeypatch):
    home = tmp_path / "pb-malformed"
    home.mkdir()
    (home / "ml-state.json").write_text("{not json")
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/predictor-breakdown")
    assert r.status_code == 500
    assert "error" in r.json()


def test_reliability_empty_when_systemctl_unavailable(client, monkeypatch):
    """Patch subprocess.run to raise → uptime/restart_count stay None, no crash."""
    import subprocess as _sp

    def _raise(*_args, **_kw):
        raise FileNotFoundError("systemctl: no such binary")

    monkeypatch.setattr(_sp, "run", _raise)
    r = client.get("/api/reliability")
    assert r.status_code == 200
    body = r.json()
    assert body["uptime_sec"] is None
    assert body["restart_count"] is None
    assert body["last_crash"] is None
    # MTBF falls back to None when both uptime and last_crash are absent.
    assert body["mtbf_sec"] is None


def test_reliability_parses_systemctl_output(client, monkeypatch):
    """Patch subprocess.run to return mock systemctl output → uptime_sec > 0."""
    import subprocess as _sp

    class _MockCP:
        returncode = 0
        # 100 seconds since boot, in microseconds.
        stdout = b"ActiveEnterTimestampMonotonic=100000000\nNRestarts=3\n"

    def _fake_run(*_args, **_kw):
        return _MockCP()

    # Make /proc/uptime read return a value greater than the start_mono so
    # uptime_sec is positive. /proc/uptime exists on the host already, but
    # we monkey-patch the open() inside the route to control the value.
    real_open = open

    def _fake_open(path, *args, **kw):
        if path == "/proc/uptime":
            from io import StringIO
            return StringIO("200.50 0.00\n")
        return real_open(path, *args, **kw)

    monkeypatch.setattr(_sp, "run", _fake_run)
    monkeypatch.setattr("builtins.open", _fake_open)
    r = client.get("/api/reliability")
    assert r.status_code == 200
    body = r.json()
    # 200.5 - 100 = 100.5 seconds uptime
    assert body["uptime_sec"] is not None
    assert body["uptime_sec"] > 0
    assert abs(body["uptime_sec"] - 100.5) < 0.01
    assert body["restart_count"] == 3
    # No crash file → MTBF mirrors uptime (best-case lower bound)
    assert body["mtbf_sec"] == body["uptime_sec"]


def test_reliability_includes_last_crash_when_file_present(tmp_path, monkeypatch):
    """data/last-crash-recovery.json present → response.last_crash carries kind."""
    import json as _json
    import time as _time

    home = tmp_path / "reliability-crash"
    home.mkdir()
    (home / "last-crash-recovery.json").write_text(_json.dumps({
        "ts": _time.time() - 3600.0,
        "kind": "baseline_restore",
        "armed_count": 1,
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    r = fresh.get("/api/reliability")
    assert r.status_code == 200
    body = r.json()
    assert body["last_crash"] is not None
    assert body["last_crash"]["kind"] == "baseline_restore"
    # ~ 1 hour ago, allow generous slack.
    assert 3500.0 < body["last_crash"]["age_sec"] < 3700.0


def test_stress_runs_malformed_json_returns_empty(client, tmp_path, monkeypatch):
    """Malformed index.json → empty response, no crash."""
    bench_dir = tmp_path / "bench_runs_bad"
    bench_dir.mkdir()
    (bench_dir / "index.json").write_text("{not valid json")
    monkeypatch.setattr("coolstep.dashboard.server._bench_runs_path", lambda: bench_dir)

    r = client.get("/api/stress-runs")
    assert r.status_code == 200
    assert r.json() == {"runs": [], "count": 0}


def test_predictor_cockpit_forecasts_three_horizons(tmp_path, monkeypatch):
    """P2.9.3: /api/predictor-cockpit ships forecasts dict {h5,h15,h30}
    sampled from the same meta-anchored saturation curve.  At h=30s the
    sample equals `expected_temp_c` exactly; h=5s and h=15s lie between
    current and predicted along the τ=4s Newton saturation."""
    import json as _json
    import math

    home = tmp_path / "cockpit-forecasts"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "knn_v1+meta",
        "throttle_prob": 0.0,
        "confidence": 0.9,
        "expected_temp_c": 80.0,
        "horizon_sec": 30.0,
        "reason": "synthetic",
        "features": {"cpu_temp_now": 60.0, "cpu_temp_max": 60.0},
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    body = fresh.get("/api/predictor-cockpit").json()
    forecasts = body["current"]["forecasts"]
    assert set(forecasts) == {"h5", "h15", "h30"}
    # h30 exactly equals expected_temp_c
    assert forecasts["h30"] == 80.0
    # h5 and h15 lie monotonically between cur (60) and predicted (80)
    assert 60.0 < forecasts["h5"] < forecasts["h15"] < forecasts["h30"]
    # h5 ≈ 60 + 20 · (1−e^−5/4)/(1−e^−30/4) — sanity check ±0.1
    f5 = (1 - math.exp(-5 / 4)) / (1 - math.exp(-30 / 4))
    assert abs(forecasts["h5"] - (60.0 + 20.0 * f5)) < 0.1


def test_predictor_cockpit_forecasts_short_horizon_no_extrapolation(
    tmp_path, monkeypatch
):
    """Regression (user-observed 2026-05-13): at horizon_sec=5 the dashboard
    rendered h5=predicted, h30=predicted (hardcoded), but h15 extrapolated
    beyond the model's horizon — producing a non-monotonic curve where h15
    overshot below both endpoints (e.g. cur=74, pred=56.5 → h15=50.05).

    Contract: when the requested horizon (5/15/30) exceeds the model's
    horizon_sec, the cockpit must NOT silently extrapolate. It either
    returns the predicted value (clamped at horizon_sec) or marks the
    sample as extrapolated. Beyond-horizon samples must equal predicted
    so the visible curve stays monotonic between cur_t and predicted."""
    import json as _json

    home = tmp_path / "cockpit-short-horizon"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "always_idle_baseline+meta",
        "throttle_prob": 0.0,
        "confidence": 0.4,
        "expected_temp_c": 56.5,
        "horizon_sec": 5.0,
        "reason": "synthetic short-horizon",
        "features": {"cpu_temp_now": 74.0, "cpu_temp_max": 74.0},
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    body = fresh.get("/api/predictor-cockpit").json()
    forecasts = body["current"]["forecasts"]
    # At-horizon sample equals predicted.
    assert forecasts["h5"] == 56.5
    # Beyond-horizon samples MUST be clamped (== predicted) — not extrapolated.
    # The pre-fix curve produced h15 = 74 + (-17.5)*1.369 ≈ 50.05 (overshoot).
    assert forecasts["h15"] == 56.5, (
        f"h15={forecasts['h15']} extrapolated beyond horizon=5s "
        f"(must clamp to predicted=56.5)"
    )
    assert forecasts["h30"] == 56.5
    # Monotonicity invariant: between cur_t (74) and predicted (56.5),
    # all samples must lie within [predicted, cur_t] (cooling case).
    for k in ("h5", "h15", "h30"):
        assert 56.5 <= forecasts[k] <= 74.0, f"{k}={forecasts[k]} out of bounds"


def test_predictor_cockpit_signed_median_field_present(tmp_path, monkeypatch):
    """P2.9.2: median_signed_err_c travels alongside median_abs_err_c."""
    import json as _json

    home = tmp_path / "cockpit-signed"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "knn_v1+meta",
        "throttle_prob": 0.0,
        "confidence": 0.5,
        "expected_temp_c": 70.0,
        "horizon_sec": 30.0,
        "reason": "synthetic",
        "features": {"cpu_temp_now": 55.0, "cpu_temp_max": 55.0},
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    body = fresh.get("/api/predictor-cockpit").json()
    assert "median_signed_err_c" in body
    assert "median_abs_err_c" in body


def test_predictor_cockpit_no_forecasts_when_no_predicted(tmp_path, monkeypatch):
    """If ml-state has no expected_temp_c, forecasts is None."""
    import json as _json

    home = tmp_path / "cockpit-no-pred"
    home.mkdir()
    (home / "ml-state.json").write_text(_json.dumps({
        "model_name": "always_idle_baseline",
        "throttle_prob": 0.0,
        "confidence": 0.0,
        "reason": "cold",
        "features": {"cpu_temp_now": 50.0, "cpu_temp_max": 50.0},
    }))
    monkeypatch.setenv("COOLSTEP_HOME", str(home))

    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app
    fresh = TestClient(create_app())
    body = fresh.get("/api/predictor-cockpit").json()
    assert body["current"]["forecasts"] is None
