"""Tests for the 2026-07-10 logic-flaw-audit fix pack.

Covers: daily_rollup ledger, orphan-relabel HOT/COOL split, py3.14 chroma
guard, S15 baseline/revert guards, daemon calibration report in ml-state,
dashboard /api/calibration daemon-report preference.
"""

from __future__ import annotations

import datetime as dt
import json

from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    ProcSig,
    TelemetryFrame,
    WorkloadFrame,
)
from coolstep.core.store import Store


def _make_frame(ts: float, *, tctl: float = 70.0, label: str | None = None,
                rpm: int = 2900, power: float = 25.0) -> TelemetryFrame:
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(
            freq_mhz=[3000.0],
            load_pct=[10.0],
            temps_c={"tctl": tctl},
            power_w={"package": power},
        ),
        gpus=[GpuMetrics(name="dgpu", temp_c=45.0, power_w=20.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=rpm)],
        workload=WorkloadFrame(
            top_processes=[ProcSig(pid=1, name="p", cpu_pct=20.0, rss_mb=10.0)],
            label=label,
        ),
    )


def _day_ts(day: dt.date, hour: int = 12) -> float:
    return dt.datetime.combine(
        day, dt.time(hour=hour), tzinfo=dt.timezone.utc
    ).timestamp()


# ── daily_rollup ─────────────────────────────────────────────────────────


def test_rollup_aggregates_complete_days_only(tmp_path):
    store = Store(tmp_path / "store.db")
    today = dt.datetime.now(dt.timezone.utc).date()
    d1 = today - dt.timedelta(days=2)
    d2 = today - dt.timedelta(days=1)
    for h in (1, 2, 3):
        store.write_frame(_make_frame(_day_ts(d1, h), tctl=70.0 + h, label="game"))
        store.write_frame(_make_frame(_day_ts(d2, h), tctl=60.0 + h, label="code"))
    # today's frames must NOT roll up (incomplete day)
    store.write_frame(_make_frame(_day_ts(today, 1), tctl=90.0, label="game"))

    written = store.rollup_days()
    assert written > 0
    rows = store.read_rollup()
    days = {r["day"] for r in rows}
    assert d1.isoformat() in days and d2.isoformat() in days
    assert today.isoformat() not in days
    # pooled row exists alongside per-label rows
    labels_d1 = {r["workload_label"] for r in rows if r["day"] == d1.isoformat()}
    assert "_all" in labels_d1 and "game" in labels_d1
    store.close()


def test_rollup_high_water_makes_rerun_noop(tmp_path):
    store = Store(tmp_path / "store.db")
    d1 = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    store.write_frame(_make_frame(_day_ts(d1), tctl=75.0, label="game"))
    assert store.rollup_days() > 0
    assert store.rollup_days() == 0  # high-water mark advanced
    store.close()


def test_rollup_survives_frame_eviction(tmp_path):
    """The whole point: ledger rows persist after frames age out of TTL."""
    import coolstep.core.store as store_mod
    store = Store(tmp_path / "store.db")
    d1 = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    store.write_frame(_make_frame(_day_ts(d1), tctl=75.0, label="game"))
    store.rollup_days()
    # evict everything
    store.rotate(now=_day_ts(d1) + store_mod.FRAMES_TTL_SEC + 86400.0)
    assert store.count_frames() == 0
    rows = store.read_rollup(workload_label="game")
    assert len(rows) == 1
    assert rows[0]["cpu_temp_p50"] == 75.0
    store.close()


def test_rollup_counts_throttle_events_on_all_row(tmp_path):
    store = Store(tmp_path / "store.db")
    d1 = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    store.write_frame(_make_frame(_day_ts(d1), tctl=91.0, label="game"))
    store.write_throttle_event(
        ts_start=_day_ts(d1, 13), ts_end=_day_ts(d1, 13) + 10.0,
        peak_temp=93.0, cause_label="thermal_pressure_93c",
        workload_at_start="game",
    )
    store.rollup_days()
    all_row = store.read_rollup(workload_label="_all")[0]
    game_row = store.read_rollup(workload_label="game")[0]
    assert all_row["throttle_events"] == 1
    assert game_row["throttle_events"] == 0
    store.close()


# ── orphan relabel ───────────────────────────────────────────────────────


class _FakeChroma:
    available = True

    def __init__(self, orphans):
        self._orphans = orphans
        self.updates: list[tuple[float, dict]] = []

    def list_unlabeled(self, before_ts, limit=2000):
        return self._orphans

    def update_metadata(self, ts, meta):
        self.updates.append((ts, meta))


def test_orphan_relabel_hot_band_labels_hot(tmp_path, monkeypatch):
    """Frames at/above HOT_THRESHOLD_C at shutdown must recover as HOT —
    the live labeler's lookahead includes the frame itself, so peak >= own
    temp is provable. Blanket-COOL poisoned the 82-90°C band."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    from coolstep.core.schema import LABEL_COOL, LABEL_HOT
    from coolstep.daemon import Daemon

    d = Daemon(
        period_sec=0.05, ring_capacity=10,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    fake = _FakeChroma([
        {"ts": 10.0, "metadata": {"cpu_temp_at": 85.0}},   # hot band
        {"ts": 11.0, "metadata": {"cpu_temp_at": 60.0}},   # cool
    ])
    d.chroma = fake
    recovered = d._recover_orphan_labels()
    assert recovered == 2
    by_ts = {ts: meta for ts, meta in fake.updates}
    assert by_ts[10.0]["was_hot_in_30s"] == LABEL_HOT
    assert by_ts[11.0]["was_hot_in_30s"] == LABEL_COOL
    assert by_ts[10.0]["peak_temp_after"] == 85.0


# ── py3.14 chroma guard ──────────────────────────────────────────────────


def test_chroma_discover_refuses_on_py314(monkeypatch, tmp_path):
    import coolstep.adapters.storage.chroma as chroma_mod

    monkeypatch.delenv("COOLSTEP_CHROMA_DISABLED", raising=False)
    monkeypatch.delenv("COOLSTEP_CHROMA_FORCE", raising=False)

    class _V:
        major, minor = 3, 14

        def __ge__(self, other):
            return (3, 14) >= other

    monkeypatch.setattr(chroma_mod.sys, "version_info", (3, 14, 0), raising=False)
    store = chroma_mod.ChromaStore(persist_dir=tmp_path / "chroma")
    assert store.discover() is False
    assert store.available is False


# ── S15 baseline / revert guards ─────────────────────────────────────────


def _mk_actuator():
    from coolstep.adapters.actuators.asusctl_fan_curve import AsusctlFanCurve
    return AsusctlFanCurve()


def test_ensure_baseline_does_not_adopt_own_curve(monkeypatch):
    from coolstep.core.curve import curve_signature

    act = _mk_actuator()
    user_curve = [(50.0, 10.0), (70.0, 40.0), (90.0, 100.0)]
    our_curve = [(50.0, 20.0), (70.0, 55.0), (90.0, 100.0)]
    act._baseline_anchors = list(user_curve)
    act._baseline_at = 0.0  # force TTL lapse
    act._last_curve_sig = curve_signature(our_curve)
    monkeypatch.setattr(act, "_read_current_curve", lambda: list(our_curve))

    baseline = act._ensure_baseline()
    assert baseline == user_curve  # kept user's, not our own bias


def test_revert_cedes_on_external_curve_change(monkeypatch):
    from coolstep.core.curve import curve_signature

    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "1")  # guard probes only when armed
    act = _mk_actuator()
    saved_baseline = [(50.0, 10.0), (70.0, 40.0), (90.0, 100.0)]
    our_curve = [(50.0, 20.0), (70.0, 55.0), (90.0, 100.0)]
    user_new = [(50.0, 5.0), (70.0, 30.0), (90.0, 100.0)]
    act._baseline_anchors = list(saved_baseline)
    act._last_curve_sig = curve_signature(our_curve)
    monkeypatch.setattr(act, "_read_current_curve", lambda: list(user_new))

    calls: list[list[str]] = []

    def _no_subprocess(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("revert must not write when ceding")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    act.revert()
    assert calls == []
    assert act._baseline_anchors is None  # stale snapshot dropped


# ── calibration unification ──────────────────────────────────────────────


def test_daemon_dumps_calibration_report(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    from coolstep.daemon import Daemon

    d = Daemon(
        period_sec=0.05, ring_capacity=10,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    d._refresh_calibration()
    assert d._last_calibration_report is not None
    assert "gates" in d._last_calibration_report
    assert "ready" in d._last_calibration_report
    # not-ready on an empty store, obviously
    assert d._last_calibration_report["ready"] is False


def test_dashboard_prefers_daemon_calibration_report(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    from coolstep.dashboard.server import _read_daemon_calibration

    ml_state = tmp_path / "ml-state.json"
    report = {
        "ready": False,
        "gates": [
            {"name": "coverage_hours", "passed": False, "current": 0.0,
             "target": 168.0, "unit": "hours", "note": ""},
        ],
        "drift_severity": 0.0,
    }
    ml_state.write_text(json.dumps({"calibration": report}))

    body = _read_daemon_calibration()
    assert body is not None
    assert body["source"] == "daemon"
    assert "coverage_hours" in body["gates"]


def test_dashboard_falls_back_when_report_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    from coolstep.dashboard.server import _read_daemon_calibration

    (tmp_path / "ml-state.json").write_text(json.dumps({"tick": 1}))
    assert _read_daemon_calibration() is None
