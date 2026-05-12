"""Calibration gate evaluator tests."""

from __future__ import annotations

from coolstep.core.calibration import evaluate
from coolstep.core.ring import Ring
from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    TelemetryFrame,
    WorkloadFrame,
)
from coolstep.core.store import Store


def _frame(ts: float, *, tctl: float = 70.0, label: str | None = None) -> TelemetryFrame:
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(temps_c={"tctl": tctl}, power_w={"package": 30.0}),
        gpus=[GpuMetrics(name="g", temp_c=50.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=2000)],
        workload=WorkloadFrame(label=label) if label else None,
    )


def test_evaluate_no_store_returns_all_failing(tmp_path):
    report = evaluate(store_path=tmp_path / "absent.db")
    assert not report.ready
    assert all(not g.passed for g in report.gates)


def test_evaluate_with_minimal_store_some_gates_pass_some_fail(tmp_path):
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    base = 1_700_000_000.0
    for i in range(10):
        store.write_frame(_frame(base + i, tctl=70.0 + i, label="idle"))
    store.close()

    report = evaluate(store_path=store_path)
    gate_map = {g.name: g for g in report.gates}
    # coverage 10s — fail
    assert gate_map["coverage_hours"].passed is False
    # peak 79°C — fail (target 85)
    assert gate_map["peak_amplitude"].passed is False
    # 1 distinct label, fail (target 5)
    assert gate_map["class_diversity"].passed is False


def test_evaluate_with_high_peak_passes_amplitude(tmp_path):
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    store.write_frame(_frame(1.0, tctl=88.0))
    store.close()
    report = evaluate(store_path=store_path)
    gate = next(g for g in report.gates if g.name == "peak_amplitude")
    assert gate.passed


def test_evaluate_with_throttle_events_passes_that_gate(tmp_path):
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    store.write_frame(_frame(1.0))
    for i in range(12):
        store.write_event = None  # noqa: SLF001 — placeholder
        store.write_throttle_event(
            ts_start=10.0 + i, ts_end=12.0 + i, peak_temp=92.0,
            cause_label="cpu_thermal", workload_at_start=None,
        )
    store.close()
    report = evaluate(store_path=store_path)
    gate = next(g for g in report.gates if g.name == "throttle_events")
    assert gate.passed
    assert gate.current == 12.0


def test_evaluate_with_diverse_labels(tmp_path):
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    for i, label in enumerate(["idle", "build", "game", "video", "browser", "ml"]):
        store.write_frame(_frame(1.0 + i, label=label))
    store.close()
    report = evaluate(store_path=store_path)
    gate = next(g for g in report.gates if g.name == "class_diversity")
    assert gate.passed


def test_evaluate_includes_optional_cost_and_ring_gates(tmp_path):
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    store.write_frame(_frame(1.0))
    store.close()

    ring = Ring(capacity=10)
    for i in range(8):
        ring.push(_frame(1.0 + i))
    report = evaluate(
        store_path=store_path,
        ring=ring,
        cost_rss_kb=10_000,
        cost_cpu_pct=0.5,
    )
    gates = {g.name for g in report.gates}
    assert "cost_rss" in gates
    assert "cost_cpu" in gates
    assert "ring_warmup" in gates
    cpu_gate = next(g for g in report.gates if g.name == "cost_cpu")
    assert cpu_gate.passed


def test_report_to_dict_serializable(tmp_path):
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    store.write_frame(_frame(1.0))
    store.close()
    report = evaluate(store_path=store_path)
    body = report.to_dict()
    assert "ready" in body
    assert isinstance(body["gates"], list)
    assert all("name" in g for g in body["gates"])  # type: ignore[union-attr]


# Regression tests for incident-2026-05-10 calibration BLOCKER fixes.

def test_coverage_hours_is_cumulative_count_not_min_max_span(tmp_path):
    """coverage_hours = count(frames) × period, NOT MAX(ts) - MIN(ts).
    Frames stretched over wide ts range with gaps must NOT report full span."""
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    # Only 5 frames, but spread across 200 seconds (gap simulates suspend).
    base = 1_700_000_000.0
    store.write_frame(_frame(base, tctl=70.0, label="x"))
    store.write_frame(_frame(base + 1, tctl=70.0, label="x"))
    store.write_frame(_frame(base + 100, tctl=70.0, label="x"))
    store.write_frame(_frame(base + 199, tctl=70.0, label="x"))
    store.write_frame(_frame(base + 200, tctl=70.0, label="x"))
    store.close()
    report = evaluate(store_path=store_path)
    gate = next(g for g in report.gates if g.name == "coverage_hours")
    # Old behavior would have reported 200/3600 = 0.055h.
    # New behavior reports 5 frames × 1.0s / 3600 ≈ 0.00139h.
    assert gate.current < 0.01, f"coverage should be ~0.0014h, got {gate.current}"


def test_hyprctl_consistency_excludes_zero_temp_frames(tmp_path):
    """Frames with cpu_temp <= 0 (collector flap, locked screen edge) must
    NOT count as misses in hyprctl_consistency. Denominator = active frames."""
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    base = 1_700_000_000.0
    # 10 active labelled frames + 90 zero-temp frames (collector down).
    for i in range(10):
        store.write_frame(_frame(base + i, tctl=70.0, label="firefox"))
    for i in range(90):
        store.write_frame(_frame(base + 100 + i, tctl=0.0, label=None))
    store.close()
    report = evaluate(store_path=store_path)
    gate = next(g for g in report.gates if g.name == "hyprctl_consistency")
    # With cpu_temp=0 frames excluded, miss% = 0% (10 active, 10 labelled).
    assert gate.current == 0.0
    assert gate.passed


def test_class_diversity_excludes_unknown_label(tmp_path):
    """The 'unknown' fallback from hyprctl is NOT a real workload class."""
    store_path = tmp_path / "store.db"
    store = Store(store_path)
    # 4 real distinct + 1 'unknown' should still report 4, not 5.
    for i, label in enumerate(["firefox", "kitty", "discord", "telegram", "unknown"]):
        store.write_frame(_frame(1.0 + i, label=label))
    store.close()
    report = evaluate(store_path=store_path)
    gate = next(g for g in report.gates if g.name == "class_diversity")
    assert gate.current == 4.0
    assert not gate.passed  # target 5


def test_env_overrides_calibration_targets(tmp_path, monkeypatch):
    """COOLSTEP_* env vars override defaults so a shadow-pilot can ramp
    gates before the full week-long calibration window completes."""
    monkeypatch.setenv("COOLSTEP_COVERAGE_HOURS", "0.0001")
    monkeypatch.setenv("COOLSTEP_THROTTLE_EVENTS", "1")
    monkeypatch.setenv("COOLSTEP_PEAK_TEMP", "60")
    monkeypatch.setenv("COOLSTEP_WORKLOAD_CLUSTERS", "2")
    monkeypatch.setenv("COOLSTEP_HYPRCTL_MISS_PCT", "50")
    # Reload module so it re-reads env at import-time constants.
    import importlib

    import coolstep.core.calibration as cal_mod
    importlib.reload(cal_mod)

    store_path = tmp_path / "store.db"
    store = Store(store_path)
    for i, label in enumerate(["firefox", "kitty"]):
        store.write_frame(_frame(1.0 + i, tctl=65.0, label=label))
    store.write_throttle_event(
        ts_start=10.0, ts_end=12.0, peak_temp=92.0,
        cause_label="cpu_thermal", workload_at_start=None,
    )
    store.close()
    report = cal_mod.evaluate(store_path=store_path)
    # All five gates should pass with these relaxed targets.
    failed = [g.name for g in report.gates if not g.passed]
    assert failed == [], f"unexpectedly failing under shadow-pilot env: {failed}"
    # restore defaults for other tests
    for key in ["COOLSTEP_COVERAGE_HOURS", "COOLSTEP_THROTTLE_EVENTS",
                "COOLSTEP_PEAK_TEMP", "COOLSTEP_WORKLOAD_CLUSTERS",
                "COOLSTEP_HYPRCTL_MISS_PCT"]:
        monkeypatch.delenv(key, raising=False)
    importlib.reload(cal_mod)
