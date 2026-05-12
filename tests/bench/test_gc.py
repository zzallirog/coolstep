"""tests/bench/test_gc.py — unit tests for bench/gc.py."""

import importlib.util
import json
import time
from pathlib import Path

import pytest

# Load bench/gc.py without requiring it to be a package.
_GC_PATH = Path(__file__).resolve().parents[2] / "bench" / "gc.py"
_spec = importlib.util.spec_from_file_location("gc_mod", _GC_PATH)
gc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc_mod)


def _make_run(runs_dir: Path, name: str, mtime_offset: float = 0.0) -> Path:
    """Create a minimal run directory with a timestamp-prefixed name."""
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True)
    # Touch so mtime is controlled via mtime_offset relative to now.
    t = time.time() + mtime_offset
    import os
    os.utime(run_dir, (t, t))
    return run_dir


def _write_meta(run_dir: Path, scenario="S1", armed=False, duration_sec=120, started_at=None):
    started_at = started_at or time.time()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "armed": armed,
                "duration_sec": duration_sec,
                "started_at": started_at,
            }
        )
    )
    return started_at


def _write_telemetry(run_dir: Path, rows: list[dict]):
    lines = "\n".join(json.dumps(r) for r in rows)
    (run_dir / "telemetry.jsonl").write_text(lines)


def _write_ml_state(run_dir: Path, rows: list[dict]):
    lines = "\n".join(json.dumps(r) for r in rows)
    (run_dir / "ml-state.jsonl").write_text(lines)


# ---------------------------------------------------------------------------
# Test 1: keeps newest N, deletes oldest
# ---------------------------------------------------------------------------

def test_gc_keeps_newest_n(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    names = [f"2026051{i}T00000{i}-S1" for i in range(5)]
    for i, name in enumerate(names):
        _make_run(runs_dir, name, mtime_offset=float(i))  # older → newer

    rc = gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "3"])
    assert rc == 0

    surviving = sorted(d.name for d in runs_dir.iterdir() if d.is_dir())
    assert len(surviving) == 3
    # The 3 newest (largest mtime_offset) should survive.
    assert names[2] in surviving
    assert names[3] in surviving
    assert names[4] in surviving
    # The 2 oldest should be gone.
    assert names[0] not in surviving
    assert names[1] not in surviving


# ---------------------------------------------------------------------------
# Test 2: --dry-run does not delete anything
# ---------------------------------------------------------------------------

def test_gc_dry_run_does_not_delete(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    for i in range(5):
        _make_run(runs_dir, f"2026051{i}T000000-S1", mtime_offset=float(i))

    rc = gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "2", "--dry-run"])
    assert rc == 0

    surviving = [d for d in runs_dir.iterdir() if d.is_dir()]
    assert len(surviving) == 5


# ---------------------------------------------------------------------------
# Test 3: index.json is written with correct count
# ---------------------------------------------------------------------------

def test_gc_writes_index_json(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    for i in range(4):
        _make_run(runs_dir, f"2026051{i}T000000-S1", mtime_offset=float(i))

    rc = gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "3"])
    assert rc == 0

    index_p = runs_dir / "index.json"
    assert index_p.exists()
    data = json.loads(index_p.read_text())
    assert data["count"] == 3
    assert len(data["runs"]) == 3
    assert "rebuilt_at" in data


# ---------------------------------------------------------------------------
# Test 4: metrics parsed correctly from real data
# ---------------------------------------------------------------------------

def test_gc_parses_metrics_correctly(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    run_dir = _make_run(runs_dir, "20260512T010000-S1")
    base_ts = 1_700_000_000.0
    _write_meta(run_dir, scenario="S1", started_at=base_ts)
    _write_telemetry(
        run_dir,
        [
            {"cpu_temp": 72.0, "fan_max_rpm": 3000, "recorded_at": base_ts + 5},
            {"cpu_temp": 91.5, "fan_max_rpm": 3000, "recorded_at": base_ts + 10},
            {"cpu_temp": 88.0, "fan_max_rpm": 3200, "recorded_at": base_ts + 12},
        ],
    )
    (run_dir / "ml-state.jsonl").write_text("")

    rc = gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "5"])
    assert rc == 0

    data = json.loads((runs_dir / "index.json").read_text())
    run_rec = data["runs"][0]
    assert run_rec["scenario"] == "S1"
    assert run_rec["peak_tctl"] == pytest.approx(91.5)
    assert run_rec["time_above_85"] == 4  # 2 rows × 2s
    assert run_rec["first_rampup_age"] is None  # no RPM ≥ 4500


# ---------------------------------------------------------------------------
# Test 5: verdict coolstep_moved_first (delta = 13 s ≥ 8)
# ---------------------------------------------------------------------------

def test_gc_verdict_moved_first(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    run_dir = _make_run(runs_dir, "20260512T020000-S1")
    base_ts = 1_700_000_000.0
    _write_meta(run_dir, started_at=base_ts)

    # first_predict_age = 2 s (prob 0.8 at base+2)
    _write_ml_state(
        run_dir,
        [{"throttle_prob": 0.8, "recorded_at": base_ts + 2}],
    )
    # first_rampup_age = 15 s (RPM ≥ 4500 at base+15)
    _write_telemetry(
        run_dir,
        [
            {"cpu_temp": 80.0, "fan_max_rpm": 3000, "recorded_at": base_ts + 5},
            {"cpu_temp": 90.0, "fan_max_rpm": 5000, "recorded_at": base_ts + 15},
        ],
    )

    gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "5"])
    data = json.loads((runs_dir / "index.json").read_text())
    run_rec = data["runs"][0]
    assert run_rec["verdict"] == "coolstep_moved_first"
    assert run_rec["delta_sec"] == pytest.approx(13.0)


# ---------------------------------------------------------------------------
# Test 6: verdict did_not_move_first (delta = 2 s < 8)
# ---------------------------------------------------------------------------

def test_gc_verdict_did_not_move(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    run_dir = _make_run(runs_dir, "20260512T030000-S1")
    base_ts = 1_700_000_000.0
    _write_meta(run_dir, started_at=base_ts)

    # first_predict_age = 10 s
    _write_ml_state(
        run_dir,
        [{"throttle_prob": 0.9, "recorded_at": base_ts + 10}],
    )
    # first_rampup_age = 12 s  → delta = 2 s < 8
    _write_telemetry(
        run_dir,
        [{"cpu_temp": 88.0, "fan_max_rpm": 5500, "recorded_at": base_ts + 12}],
    )

    gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "5"])
    data = json.loads((runs_dir / "index.json").read_text())
    run_rec = data["runs"][0]
    assert run_rec["verdict"] == "did_not_move_first"
    assert run_rec["delta_sec"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Test 7: verdict not_observed when both jsonl files are empty
# ---------------------------------------------------------------------------

def test_gc_verdict_not_observed(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    run_dir = _make_run(runs_dir, "20260512T040000-S1")
    _write_meta(run_dir, started_at=1_700_000_000.0)
    (run_dir / "telemetry.jsonl").write_text("")
    (run_dir / "ml-state.jsonl").write_text("")

    gc_mod.main(["--runs-dir", str(runs_dir), "--keep", "5"])
    data = json.loads((runs_dir / "index.json").read_text())
    run_rec = data["runs"][0]
    assert run_rec["verdict"] == "not_observed"
    assert run_rec["delta_sec"] is None
