"""Snapshot archive tests — P2.5 Heavy-4.

Covers:
- write/read round-trip (single + multiple)
- newest-first ordering
- non-existent file returns []
- malformed jsonl line is skipped, not fatal
- compute_drift positive (chip hotter than baseline)
- compute_drift negative (chip cooler than baseline)
- compute_drift zero (identical)
- DISABLED_ENV suppresses writes
"""

from __future__ import annotations

import json

import pytest

from coolstep.core.snapshot_archive import (
    DISABLED_ENV,
    REASON_CALIBRATION_PASS,
    REASON_MANUAL,
    REASON_POST_CLEANING,
    Snapshot,
    compute_drift,
    read_snapshots,
    write_snapshot,
)


@pytest.fixture(autouse=True)
def _no_pollution(monkeypatch, tmp_path):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv(DISABLED_ENV, raising=False)


def _make(
    *,
    ts: float = 1000.0,
    reason: str = REASON_MANUAL,
    idle: float = 45.0,
    load60: float = 65.0,
    load100: float = 85.0,
    n: int = 100,
    note: str = "",
    stats: dict | None = None,
) -> Snapshot:
    return Snapshot(
        ts=ts,
        reason=reason,
        median_embedder_stats=stats if stats is not None else {
            "cpu_temp_max": {"median": 55.0, "mad": 8.0},
            "cpu_load_max": {"median": 30.0, "mad": 20.0},
        },
        median_idle_temp_c=idle,
        median_load60_temp_c=load60,
        median_load100_temp_c=load100,
        sample_n=n,
        note=note,
    )


# ── write / read round-trip ──────────────────────────────────────────


def test_write_and_read_single(tmp_path):
    path = tmp_path / "snapshot_archive.jsonl"
    snap = _make(ts=1234.5, reason=REASON_POST_CLEANING, note="fresh paste")
    write_snapshot(snap, path=path)
    rows = read_snapshots(path=path)
    assert len(rows) == 1
    got = rows[0]
    assert got.ts == 1234.5
    assert got.reason == REASON_POST_CLEANING
    assert got.note == "fresh paste"
    assert got.median_idle_temp_c == 45.0
    assert got.median_load60_temp_c == 65.0
    assert got.median_load100_temp_c == 85.0
    assert got.sample_n == 100
    assert got.median_embedder_stats["cpu_temp_max"]["median"] == 55.0


def test_write_multiple_newest_first(tmp_path):
    path = tmp_path / "snapshot_archive.jsonl"
    write_snapshot(_make(ts=100.0), path=path)
    write_snapshot(_make(ts=300.0), path=path)
    write_snapshot(_make(ts=200.0), path=path)
    rows = read_snapshots(path=path)
    assert [r.ts for r in rows] == [300.0, 200.0, 100.0]


def test_read_missing_file_returns_empty(tmp_path):
    rows = read_snapshots(path=tmp_path / "does-not-exist.jsonl")
    assert rows == []


def test_read_skips_malformed_lines(tmp_path):
    path = tmp_path / "snapshot_archive.jsonl"
    # Mix one good row + garbage + empty + good row
    write_snapshot(_make(ts=100.0), path=path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
        fh.write("\n")
        fh.write('{"this": "is not a snapshot but is valid json"}\n')
    write_snapshot(_make(ts=200.0), path=path)
    rows = read_snapshots(path=path)
    # The not-a-snapshot json row parses but defaults all fields to 0.0/""
    # → still counted, just degenerate. The garbage line is dropped.
    ts_values = [r.ts for r in rows]
    assert 100.0 in ts_values
    assert 200.0 in ts_values
    # 4 rows total: 2 real + 1 degenerate ts=0.0; garbage is dropped
    assert len(rows) == 3


def test_default_reason_constants():
    assert REASON_MANUAL == "manual"
    assert REASON_POST_CLEANING == "post_cleaning"
    assert REASON_CALIBRATION_PASS == "calibration_pass"


# ── DISABLED_ENV suppresses writes ────────────────────────────────────


def test_disabled_env_suppresses_write(monkeypatch, tmp_path):
    path = tmp_path / "snapshot_archive.jsonl"
    monkeypatch.setenv(DISABLED_ENV, "1")
    write_snapshot(_make(ts=999.0), path=path)
    assert not path.exists()
    # And read returns empty
    assert read_snapshots(path=path) == []


# ── drift computation ────────────────────────────────────────────────


def test_drift_positive_hotter_than_baseline():
    """Chip running hotter than baseline → positive deltas."""
    baseline = _make(ts=100.0, idle=40.0, load60=60.0, load100=80.0,
                     reason=REASON_POST_CLEANING)
    current = _make(ts=200.0, idle=45.0, load60=68.0, load100=87.5)
    d = compute_drift(current, baseline)
    assert d["idle"] == pytest.approx(5.0)
    assert d["load60"] == pytest.approx(8.0)
    assert d["load100"] == pytest.approx(7.5)


def test_drift_negative_cooler_than_baseline():
    """Chip running cooler than baseline (post-cleaning vs pre) → negative."""
    baseline = _make(ts=100.0, idle=50.0, load60=72.0, load100=92.0)
    current = _make(ts=200.0, idle=44.0, load60=66.0, load100=84.0,
                    reason=REASON_POST_CLEANING)
    d = compute_drift(current, baseline)
    assert d["idle"] == pytest.approx(-6.0)
    assert d["load60"] == pytest.approx(-6.0)
    assert d["load100"] == pytest.approx(-8.0)


def test_drift_zero_when_identical():
    baseline = _make(ts=100.0, idle=45.0, load60=65.0, load100=85.0)
    current = _make(ts=200.0, idle=45.0, load60=65.0, load100=85.0)
    d = compute_drift(current, baseline)
    assert d == {"idle": 0.0, "load60": 0.0, "load100": 0.0}


def test_drift_mixed_signs():
    """Idle drift can be opposite sign to load drift (ambient flipped,
    fan curve repaired). Function returns per-profile, no aggregation."""
    baseline = _make(ts=100.0, idle=50.0, load60=70.0, load100=90.0)
    current = _make(ts=200.0, idle=55.0, load60=65.0, load100=88.0)
    d = compute_drift(current, baseline)
    assert d["idle"] == pytest.approx(5.0)   # hotter at idle
    assert d["load60"] == pytest.approx(-5.0)  # cooler at load60
    assert d["load100"] == pytest.approx(-2.0)


# ── stats round-trip in jsonl ────────────────────────────────────────


def test_embedder_stats_round_trip(tmp_path):
    path = tmp_path / "snapshot_archive.jsonl"
    stats = {
        "cpu_temp_max": {"median": 62.5, "mad": 7.25},
        "fan_rpm_max": {"median": 3200.0, "mad": 800.0},
    }
    snap = _make(ts=500.0, stats=stats)
    write_snapshot(snap, path=path)
    [got] = read_snapshots(path=path)
    assert got.median_embedder_stats == stats


def test_jsonl_is_one_line_per_snapshot(tmp_path):
    """Append-only guarantee: each write adds exactly one line."""
    path = tmp_path / "snapshot_archive.jsonl"
    for i in range(5):
        write_snapshot(_make(ts=float(i)), path=path)
    with path.open("r", encoding="utf-8") as fh:
        lines = [line for line in fh.read().splitlines() if line]
    assert len(lines) == 5
    # Each line is valid JSON
    for line in lines:
        json.loads(line)
