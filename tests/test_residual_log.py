"""Residual log — append, tail, iter, rotation, corrupt-line tolerance."""

from __future__ import annotations

import json
from pathlib import Path

from coolstep.core.residual_log import (
    DEFAULT_GENERATIONS,
    ResidualLog,
    ResidualRecord,
)


def _mk(ts: float, predicted: float, actual: float) -> ResidualRecord:
    return ResidualRecord.from_pair(
        ts=ts,
        predicted_at=ts - 5.0,
        horizon_sec=5.0,
        predicted_temp_c=predicted,
        actual_temp_c=actual,
        model_name="trajectory_baseline",
        profile="workstation-latency-quiet",
        bucket_key=(1, 2, 1),
        features={"cpu_temp_max": predicted - 0.1, "cpu_temp_slope_per_sec": 0.02},
    )


def test_residual_record_computes_signed_residual():
    r = _mk(ts=1000.0, predicted=77.0, actual=57.0)
    # actual - predicted = 57 - 77 = -20
    assert r.residual_c == -20.0
    r2 = _mk(ts=1000.0, predicted=50.0, actual=55.5)
    assert r2.residual_c == 5.5


def test_append_then_tail_roundtrip(tmp_path: Path):
    log = ResidualLog(tmp_path / "residuals.jsonl")
    log.append(_mk(1000.0, 77.0, 57.0))
    log.append(_mk(1005.0, 60.0, 62.0))
    log.append(_mk(1010.0, 65.0, 64.5))
    tail = log.tail(10)
    assert len(tail) == 3
    assert tail[0].predicted_temp_c == 77.0
    assert tail[-1].actual_temp_c == 64.5
    assert tail[1].residual_c == 2.0
    assert tail[0].bucket_key == (1, 2, 1)  # tuple roundtripped


def test_tail_n_returns_last_n_only(tmp_path: Path):
    log = ResidualLog(tmp_path / "r.jsonl")
    for i in range(50):
        log.append(_mk(1000.0 + i, 70.0, 70.0 + i * 0.1))
    last5 = log.tail(5)
    assert len(last5) == 5
    assert last5[0].ts == 1045.0
    assert last5[-1].ts == 1049.0


def test_tail_on_missing_file_returns_empty(tmp_path: Path):
    log = ResidualLog(tmp_path / "absent.jsonl")
    assert log.tail(10) == []
    assert log.count() == 0


def test_corrupt_trailing_line_is_skipped(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    log = ResidualLog(path)
    log.append(_mk(1000.0, 70.0, 70.0))
    log.append(_mk(1005.0, 71.0, 71.0))
    # Simulate a mid-write crash — append a truncated JSON line.
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"ts": 1010.0, "predicted_at": 100')
    tail = log.tail(10)
    assert len(tail) == 2  # the broken line is silently skipped


def test_rotation_when_size_exceeded(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    # max_bytes sized for ~6 records per generation; 20 appends → rotates ~3x.
    log = ResidualLog(path, max_bytes=2500, generations=3)
    for i in range(20):
        log.append(_mk(1000.0 + i, 70.0, 70.0 + i))
    # Live file plus up to (generations-1) archives.
    files = sorted(tmp_path.glob("r.jsonl*"))
    assert len(files) <= DEFAULT_GENERATIONS  # 3
    # iter_all walks archives oldest-first → live last; should retain at
    # least 2 generations of records (older one is dropped, plus live).
    all_records = list(log.iter_all())
    assert len(all_records) >= 6


def test_rotation_drops_oldest(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    # Force aggressive rotation to verify the oldest gets removed.
    log = ResidualLog(path, max_bytes=200, generations=3)
    for i in range(60):
        log.append(_mk(1000.0 + i, 70.0, 70.0 + i))
    files = sorted(tmp_path.glob("r.jsonl*"))
    # Should have at most live + 2 archives = 3 files total.
    assert len(files) <= 3
    # Oldest record in iter_all should not be the very first append —
    # earliest generations have been dropped.
    all_records = list(log.iter_all())
    assert all_records[0].ts > 1000.0


def test_iter_all_archives_first_live_last(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    log = ResidualLog(path, max_bytes=300, generations=3)
    for i in range(30):
        log.append(_mk(1000.0 + i, 70.0, 70.0 + i * 0.5))
    records = list(log.iter_all())
    # ts strictly increasing across the merged stream
    timestamps = [r.ts for r in records]
    assert timestamps == sorted(timestamps), "iter_all must be chronological"


def test_features_dict_roundtrip(tmp_path: Path):
    log = ResidualLog(tmp_path / "r.jsonl")
    rec = ResidualRecord.from_pair(
        ts=1.0, predicted_at=0.0, horizon_sec=1.0,
        predicted_temp_c=10.0, actual_temp_c=11.0,
        model_name="trajectory_baseline",
        features={"a": 1.5, "b": -0.25, "c": 0.0},
    )
    log.append(rec)
    out = log.tail(1)[0]
    assert out.features == {"a": 1.5, "b": -0.25, "c": 0.0}
    assert out.bucket_key is None
    assert out.profile is None


def test_intervention_fields_roundtrip(tmp_path: Path):
    log = ResidualLog(tmp_path / "r.jsonl")
    rec = ResidualRecord.from_pair(
        ts=10.0, predicted_at=5.0, horizon_sec=5.0,
        predicted_temp_c=70.0, actual_temp_c=68.0,
        model_name="trajectory_baseline",
        intervened=True,
        intervention_verbs=("ramp_cooling",),
    )
    log.append(rec)

    out = log.tail(1)[0]

    assert out.intervened is True
    assert out.intervention_verbs == ("ramp_cooling",)


def test_intervention_fields_default_for_old_records(tmp_path: Path):
    path = tmp_path / "r.jsonl"
    old_record = {
        "ts": 10.0,
        "predicted_at": 5.0,
        "horizon_sec": 5.0,
        "predicted_temp_c": 70.0,
        "actual_temp_c": 71.0,
        "residual_c": 1.0,
        "model_name": "trajectory_baseline",
        "features": {},
    }
    path.write_text(json.dumps(old_record) + "\n")

    out = ResidualLog(path).tail(1)[0]

    assert out.intervened is False
    assert out.intervention_verbs == ()


def test_count_reflects_live_file_only(tmp_path: Path):
    log = ResidualLog(tmp_path / "r.jsonl", max_bytes=300, generations=3)
    for i in range(20):
        log.append(_mk(1000.0 + i, 70.0, 70.0 + i))
    # count() counts live-file lines only.  After rotation, the live file
    # has fewer than total appended.
    n = log.count()
    assert 0 < n <= 20
