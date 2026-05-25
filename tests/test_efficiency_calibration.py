"""Tests for steady-state efficiency calibration — Phase G (Heavy 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coolstep.core.efficiency_calibration import (
    EfficiencyRow,
    analyse_window,
    append_table,
    load_table,
)
from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    TelemetryFrame,
    WorkloadFrame,
)

# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_pollution(monkeypatch, tmp_path):
    """Keep the user's real ~/coolstep/data clean across tests."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_EFFICIENCY_LOG_DISABLED", raising=False)


def _frame(
    ts: float,
    *,
    tctl: float = 75.0,
    rpm: int = 2500,
    load: float = 40.0,
    power: float | None = 35.0,
    ambient: float | None = 22.0,
    label: str = "code",
) -> TelemetryFrame:
    cpu = CpuMetrics(load_pct=[load, load], temps_c={"tctl": tctl})
    if power is not None:
        cpu.power_w["package"] = float(power)
    platform_state = {}
    if ambient is not None:
        platform_state["ambient_c"] = str(ambient)
    return TelemetryFrame(
        timestamp=ts,
        cpu=cpu,
        fans=[FanMetrics(name="cpu_fan", rpm=rpm)],
        workload=WorkloadFrame(label=label),
        platform_state=platform_state,
    )


def _stable_run(
    *,
    start_ts: float,
    n: int,
    rpm: int,
    tctl: float = 75.0,
    label: str = "code",
    power: float | None = 35.0,
    ambient: float | None = 22.0,
) -> list[TelemetryFrame]:
    """n consecutive frames at 1Hz with flat temperature."""
    return [
        _frame(start_ts + i, tctl=tctl, rpm=rpm, label=label, power=power, ambient=ambient)
        for i in range(n)
    ]


# ── analyse_window: one stable run ────────────────────────────────────


def test_one_stable_run_produces_one_row_with_correct_min_rpm():
    # 15 frames at 1 Hz — slope < tolerance, duration 14 s >= 10 s.
    frames = _stable_run(start_ts=0.0, n=15, rpm=2500)
    rows = analyse_window(frames)
    assert len(rows) == 1
    row = rows[0]
    assert row.workload_class == "code"
    assert row.power_bucket_w == 40  # 35 W rounds to bucket 40
    assert row.ambient_bucket_c == 22  # 22 °C rounds to bucket 22
    assert row.min_rpm_for_stable == 2500
    assert row.sample_count == 15
    assert row.last_seen_ts == 14.0


def test_run_below_min_stable_secs_yields_no_rows():
    # 5 frames at 1 Hz — only 4 s, below 10 s threshold.
    frames = _stable_run(start_ts=0.0, n=5, rpm=2500)
    rows = analyse_window(frames, min_stable_secs=10.0)
    assert rows == []


def test_unlabelled_frames_are_skipped():
    frames = _stable_run(start_ts=0.0, n=15, rpm=2500)
    # Strip workload label on all frames.
    for f in frames:
        f.workload = None
    rows = analyse_window(frames)
    assert rows == []


def test_missing_power_falls_into_zero_w_bucket():
    frames = _stable_run(start_ts=0.0, n=15, rpm=2200, power=None)
    rows = analyse_window(frames)
    assert len(rows) == 1
    assert rows[0].power_bucket_w == 0
    assert rows[0].min_rpm_for_stable == 2200


def test_missing_ambient_falls_into_zero_c_bucket():
    frames = _stable_run(start_ts=0.0, n=15, rpm=2300, ambient=None)
    rows = analyse_window(frames)
    assert len(rows) == 1
    assert rows[0].ambient_bucket_c == 0


def test_slope_above_tolerance_breaks_the_run():
    # Make one frame jump 5 °C — slope 5 °C/s, way above 0.05 tolerance.
    # 25 frames total so both halves individually exceed 10 s stable.
    frames = _stable_run(start_ts=0.0, n=25, rpm=2500, tctl=75.0)
    frames[12].cpu.temps_c["tctl"] = 80.0  # break point
    rows = analyse_window(frames)
    # Both halves individually qualify (12 s + 12 s of stable). They
    # share the same bucket key, so they merge into one row with the
    # same min_rpm.
    assert len(rows) == 1
    assert rows[0].min_rpm_for_stable == 2500


# ── two stable runs, same bucket, different rpm ───────────────────────


def test_two_stable_runs_same_bucket_keep_lower_min_rpm():
    """Same (workload, power, ambient) bucket, two runs with different
    fan speeds — the resulting row's min_rpm must be the lower of the
    two."""
    higher = _stable_run(start_ts=0.0, n=15, rpm=3200)
    # Mark second run far enough out that it's a separate stable run
    # but lands in the same bucket. Power bumped slightly but still
    # rounds to 40 W; ambient unchanged.
    lower = _stable_run(start_ts=100.0, n=15, rpm=2400, power=37.0)
    # Insert a slope-breaking frame between the runs (no label, so it
    # ALSO doesn't anchor a bucket; the analyser must just see two
    # independently stable runs).
    bridge = _frame(50.0, tctl=95.0, rpm=5000, label="code")
    frames = higher + [bridge] + lower

    rows = analyse_window(frames)
    bucket_rows = [r for r in rows
                   if r.workload_class == "code"
                   and r.power_bucket_w == 40
                   and r.ambient_bucket_c == 22]
    assert len(bucket_rows) == 1
    assert bucket_rows[0].min_rpm_for_stable == 2400


# ── append + load round-trip ──────────────────────────────────────────


def test_append_and_load_round_trip(tmp_path):
    path = tmp_path / "efficiency_table.jsonl"
    rows = [
        EfficiencyRow("code", 40, 22, 2400, 15, 100.0),
        EfficiencyRow("render", 60, 22, 3300, 12, 200.0),
    ]
    append_table(rows, path=path)
    out = load_table(path=path)
    assert len(out) == 2
    keys = {(r.workload_class, r.power_bucket_w, r.ambient_bucket_c) for r in out}
    assert keys == {("code", 40, 22), ("render", 60, 22)}
    rendered = next(r for r in out if r.workload_class == "render")
    assert rendered.min_rpm_for_stable == 3300


def test_append_supersedes_with_lower_min_rpm(tmp_path):
    path = tmp_path / "efficiency_table.jsonl"
    # First write: 2400 RPM
    append_table([EfficiencyRow("code", 40, 22, 2400, 15, 100.0)], path=path)
    # Second write: improved min_rpm to 2200
    append_table([EfficiencyRow("code", 40, 22, 2200, 18, 200.0)], path=path)
    # A third write at 2500 RPM (worse) — must NOT overwrite the view.
    append_table([EfficiencyRow("code", 40, 22, 2500, 30, 300.0)], path=path)

    out = load_table(path=path)
    assert len(out) == 1
    assert out[0].min_rpm_for_stable == 2200


def test_load_table_missing_file_returns_empty(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert load_table(path=path) == []


def test_append_respects_disable_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLSTEP_EFFICIENCY_LOG_DISABLED", "1")
    path = tmp_path / "efficiency_table.jsonl"
    append_table([EfficiencyRow("code", 40, 22, 2400, 15, 100.0)], path=path)
    assert not path.exists()


def test_load_table_recovers_from_corrupt_lines(tmp_path):
    path = tmp_path / "efficiency_table.jsonl"
    # Mix one valid line, one malformed.
    path.write_text(
        '{"workload_class":"code","power_bucket_w":40,'
        '"ambient_bucket_c":22,"min_rpm_for_stable":2400,'
        '"sample_count":15,"last_seen_ts":100.0}\n'
        'not-json\n',
        encoding="utf-8",
    )
    out = load_table(path=path)
    assert len(out) == 1
    assert out[0].workload_class == "code"


# ── rotation ──────────────────────────────────────────────────────────


def test_rotation_kicks_in_past_threshold(tmp_path, monkeypatch):
    """Past MAX_EFFICIENCY_BYTES, the active file rotates to .1."""
    # Patch the module-level threshold to a tiny value so any single
    # row pushes past it.
    monkeypatch.setattr(
        "coolstep.core.efficiency_calibration.MAX_EFFICIENCY_BYTES",
        10,
    )
    path: Path = tmp_path / "efficiency_table.jsonl"

    # First write — file doesn't exist yet, no rotation. Use a sequence
    # of improving min_rpm values across DIFFERENT buckets so each
    # append is accepted (the latest-per-bucket dedupe doesn't filter).
    append_table([EfficiencyRow("code", 10, 22, 2400, 5, 100.0)], path=path)
    assert path.exists()

    # Second write into a fresh bucket — file is now > 10 bytes, so the
    # _rotate() path runs before this write.
    append_table([EfficiencyRow("code", 20, 22, 2300, 5, 200.0)], path=path)

    gen1 = Path(str(path) + ".1")
    assert gen1.exists(), "rotation should produce .1 after threshold breach"
