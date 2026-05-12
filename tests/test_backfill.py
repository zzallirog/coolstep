"""Unit tests for coolstep.core.backfill.backfill_labels.

Spec — maintainer's local backfill plan (task A): re-label LABEL_UNKNOWN
chroma vectors from sqlite `throttle_events` table. Fake chroma class +
sqlite tmp file isolate the test from the real chromadb dep.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coolstep.core.backfill import backfill_labels
from coolstep.core.schema import LABEL_COOL, LABEL_HOT


class _FakeChroma:
    """Mimics ChromaStore's `list_unlabeled`/`update_metadata` surface.

    `list_unlabeled` accepts (before_ts, limit) to match the real adapter,
    but ignores them — the test pre-populates `_unlabeled` directly.
    """

    def __init__(self, unlabeled_vectors, *, available: bool = True) -> None:
        self.available = available
        self._unlabeled = list(unlabeled_vectors)
        self.updates: list[tuple[float, dict]] = []

    def list_unlabeled(self, before_ts: float = 0.0, limit: int = 0):  # noqa: ARG002
        return list(self._unlabeled)

    def update_metadata(self, ts: float, metadata: dict) -> None:
        self.updates.append((ts, dict(metadata)))


def _make_store(tmp_path: Path, events, frames=()) -> Path:
    """Create a fresh sqlite store with throttle_events + frames tables.

    Minimal schema (matches the original P1 spec). Tests that exercise the
    P2.5 danger / stable label paths use `_make_store_rich` instead — it adds
    the columns those labels consult (fan_max_rpm, raw_json).
    """
    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE throttle_events ("
        "ts_start REAL, ts_end REAL, duration REAL, peak_temp REAL, "
        "cause_label TEXT, workload_at_start TEXT)"
    )
    conn.execute("CREATE TABLE frames (ts REAL, cpu_temp REAL)")
    for ts_start, peak in events:
        conn.execute(
            "INSERT INTO throttle_events (ts_start, peak_temp) VALUES (?, ?)",
            (ts_start, peak),
        )
    for ts, cpu_temp in frames:
        conn.execute(
            "INSERT INTO frames (ts, cpu_temp) VALUES (?, ?)",
            (ts, cpu_temp),
        )
    conn.commit()
    conn.close()
    return db


def _make_store_rich(tmp_path: Path, events, frames=()) -> Path:
    """Like `_make_store` but with fan_max_rpm + raw_json columns so the
    P2.5 spike / stable detectors can read load_pct + RPM history.

    `frames` rows are (ts, cpu_temp, fan_rpm, load_max) tuples; load_max is
    embedded into a minimal raw_json with `cpu.load_pct=[load_max]` so the
    backfill's _load_max_from_raw helper picks it up.
    """
    import json as _json

    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE throttle_events ("
        "ts_start REAL, ts_end REAL, duration REAL, peak_temp REAL, "
        "cause_label TEXT, workload_at_start TEXT)"
    )
    conn.execute(
        "CREATE TABLE frames ("
        "ts REAL, cpu_temp REAL, fan_max_rpm INTEGER, raw_json TEXT)"
    )
    for ts_start, peak in events:
        conn.execute(
            "INSERT INTO throttle_events (ts_start, peak_temp) VALUES (?, ?)",
            (ts_start, peak),
        )
    for ts, cpu_temp, fan_rpm, load_max in frames:
        raw = _json.dumps({"cpu": {"load_pct": [float(load_max)]}})
        conn.execute(
            "INSERT INTO frames (ts, cpu_temp, fan_max_rpm, raw_json) "
            "VALUES (?, ?, ?, ?)",
            (ts, cpu_temp, fan_rpm, raw),
        )
    conn.commit()
    conn.close()
    return db


# 1.
def test_backfill_no_store(tmp_path: Path) -> None:
    chroma = _FakeChroma([{"ts": 100.0}])
    missing = tmp_path / "does_not_exist.db"
    stats = backfill_labels(missing, chroma)
    assert stats == {
        "unlabeled_before": 0,
        "labeled_hot": 0,
        "labeled_cool": 0,
        "danger_vectors": 0,
        "stable_vectors": 0,
        "errors": 0,
    }
    assert chroma.updates == []


# 2.
def test_backfill_chroma_unavailable(tmp_path: Path) -> None:
    db = _make_store(tmp_path, events=[(125.0, 92.0)])
    chroma = _FakeChroma([{"ts": 100.0}], available=False)
    stats = backfill_labels(db, chroma)
    assert stats["unlabeled_before"] == 0
    assert stats["labeled_hot"] == 0
    assert stats["labeled_cool"] == 0
    assert chroma.updates == []


# 3.
def test_backfill_no_unlabeled_vectors(tmp_path: Path) -> None:
    db = _make_store(tmp_path, events=[(125.0, 92.0)])
    chroma = _FakeChroma([])
    stats = backfill_labels(db, chroma)
    assert stats["unlabeled_before"] == 0
    assert stats["labeled_hot"] == 0
    assert stats["labeled_cool"] == 0
    assert chroma.updates == []


# 4.
def test_backfill_marks_hot_when_event_in_window(tmp_path: Path) -> None:
    # vector at ts=100, throttle event at ts_start=125 → within (100, 130]
    # → HOT, peak_after = event peak (92.0)
    db = _make_store(tmp_path, events=[(125.0, 92.0)])
    chroma = _FakeChroma([{"ts": 100.0}])
    stats = backfill_labels(db, chroma)
    assert stats["unlabeled_before"] == 1
    assert stats["labeled_hot"] == 1
    assert stats["labeled_cool"] == 0
    assert stats["errors"] == 0
    # P2.5-E/F: every backfill write now carries the three new keys too.
    # No frames in this fixture → no spike, no stable window.
    assert chroma.updates == [
        (100.0, {
            "was_hot_in_30s": LABEL_HOT,
            "peak_temp_after": 92.0,
            "was_danger_vector": 0,
            "is_stable": 0,
            "equilibrium_rpm": -1.0,
        }),
    ]


# 5.
def test_backfill_marks_cool_when_no_event_nearby(tmp_path: Path) -> None:
    # vector at ts=200, events at 50 and 1000 — neither falls in (200, 230]
    db = _make_store(tmp_path, events=[(50.0, 91.0), (1000.0, 95.0)])
    chroma = _FakeChroma([{"ts": 200.0}])
    stats = backfill_labels(db, chroma)
    assert stats["unlabeled_before"] == 1
    assert stats["labeled_hot"] == 0
    assert stats["labeled_cool"] == 1
    assert stats["errors"] == 0
    assert len(chroma.updates) == 1
    ts, meta = chroma.updates[0]
    assert ts == 200.0
    assert meta["was_hot_in_30s"] == LABEL_COOL
    # No frames inserted → peak stays at 0.0 default.
    assert meta["peak_temp_after"] == 0.0


# 6.
def test_backfill_idempotent(tmp_path: Path) -> None:
    db = _make_store(tmp_path, events=[(125.0, 92.0), (210.0, 88.0)])
    vectors = [{"ts": 100.0}, {"ts": 200.0}]
    chroma_a = _FakeChroma(vectors)
    chroma_b = _FakeChroma(vectors)
    stats_a = backfill_labels(db, chroma_a)
    stats_b = backfill_labels(db, chroma_b)
    assert stats_a == stats_b
    # Same set of (ts, metadata) writes (order-stable since we iterate input).
    assert chroma_a.updates == chroma_b.updates
    # And exactly the contract expected for these vectors:
    # ts=100 → event 125 in (100,130] → HOT, peak=92
    # ts=200 → event 210 in (200,230] → HOT, peak=88
    assert stats_a["labeled_hot"] == 2
    assert stats_a["labeled_cool"] == 0


# 7.
def test_backfill_peak_temp_after_from_frames_for_cool(tmp_path: Path) -> None:
    # vector ts=300, no nearby events. Frames at ts=305..320 with max 70°C.
    # Expect COOL + peak_temp_after=70.0.
    frames = [(305.0, 60.0), (310.0, 70.0), (320.0, 65.0)]
    db = _make_store(tmp_path, events=[], frames=frames)
    chroma = _FakeChroma([{"ts": 300.0}])
    stats = backfill_labels(db, chroma)
    assert stats["labeled_cool"] == 1
    assert stats["labeled_hot"] == 0
    ts, meta = chroma.updates[0]
    assert ts == 300.0
    assert meta["was_hot_in_30s"] == LABEL_COOL
    assert meta["peak_temp_after"] == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# P2.5-E — Danger vector label
# ---------------------------------------------------------------------------

# 8.
def test_danger_vector_throttle_followed_by_load_spike(tmp_path: Path) -> None:
    """Hot vector + a ≥70pp load jump between adjacent frames → danger=1."""
    # Vector at ts=100, throttle at 120 (within +30s), and the two latest
    # frames at-or-before 100 show load_max 5% → 95% (jump = 90pp ≥ 70).
    rich_frames = [
        # (ts, cpu_temp, fan_rpm, load_max)
        (99.0, 65.0, 2500, 5.0),
        (100.0, 70.0, 2500, 95.0),
    ]
    db = _make_store_rich(tmp_path, events=[(120.0, 92.0)], frames=rich_frames)
    chroma = _FakeChroma([{"ts": 100.0}])
    stats = backfill_labels(db, chroma)
    assert stats["labeled_hot"] == 1
    assert stats["danger_vectors"] == 1
    _, meta = chroma.updates[0]
    assert meta["was_danger_vector"] == 1


# 9.
def test_danger_vector_non_spike_throttle_zero(tmp_path: Path) -> None:
    """Hot vector but adjacent load stays flat (no spike) → danger=0."""
    # Load only goes 60 → 65 (delta 5 pp, well below 70).
    rich_frames = [
        (99.0, 65.0, 2500, 60.0),
        (100.0, 70.0, 2500, 65.0),
    ]
    db = _make_store_rich(tmp_path, events=[(120.0, 92.0)], frames=rich_frames)
    chroma = _FakeChroma([{"ts": 100.0}])
    stats = backfill_labels(db, chroma)
    assert stats["labeled_hot"] == 1
    assert stats["danger_vectors"] == 0
    _, meta = chroma.updates[0]
    assert meta["was_danger_vector"] == 0


# 10.
def test_danger_vector_only_when_hot(tmp_path: Path) -> None:
    """A clear load spike with no throttle in lookahead must NOT flag danger."""
    rich_frames = [
        (99.0, 60.0, 2500, 5.0),
        (100.0, 62.0, 2500, 95.0),  # +90 pp jump, but no throttle ahead.
    ]
    db = _make_store_rich(tmp_path, events=[], frames=rich_frames)
    chroma = _FakeChroma([{"ts": 100.0}])
    stats = backfill_labels(db, chroma)
    assert stats["labeled_cool"] == 1
    assert stats["danger_vectors"] == 0


# ---------------------------------------------------------------------------
# P2.5-F — Stable window + equilibrium_rpm
# ---------------------------------------------------------------------------

# 11.
def test_is_stable_detected_on_flat_window(tmp_path: Path) -> None:
    """Trailing 30s of flat temp + RPM + load → is_stable=1, eq_rpm=median."""
    # Synthetic 31 samples, all flat. Vector ts=200.
    flat = [
        (200.0 - i, 72.0, 3000, 40.0)
        for i in range(30, -1, -1)
    ]
    db = _make_store_rich(tmp_path, events=[], frames=flat)
    chroma = _FakeChroma([{"ts": 200.0}])
    stats = backfill_labels(db, chroma)
    assert stats["stable_vectors"] == 1
    _, meta = chroma.updates[0]
    assert meta["is_stable"] == 1
    assert meta["equilibrium_rpm"] == pytest.approx(3000.0)


# 12.
def test_equilibrium_rpm_only_when_stable(tmp_path: Path) -> None:
    """Climbing temperature in the trailing window → is_stable=0, eq_rpm=-1."""
    # Same frame count but temp rises ~3°C over 30s (slope=0.1 °C/s > 0.05).
    climbing = []
    for i in range(31):
        ts = 200.0 - 30 + i
        climbing.append((ts, 72.0 + 0.1 * i, 3000, 40.0))
    db = _make_store_rich(tmp_path, events=[], frames=climbing)
    chroma = _FakeChroma([{"ts": 200.0}])
    stats = backfill_labels(db, chroma)
    assert stats["stable_vectors"] == 0
    _, meta = chroma.updates[0]
    assert meta["is_stable"] == 0
    assert meta["equilibrium_rpm"] == -1.0


# 13.
def test_backfill_p25_idempotent_with_rich_frames(tmp_path: Path) -> None:
    """Re-running backfill on a rich store yields identical danger/stable
    labels (idempotency invariant from the docstring)."""
    flat = [(100.0 - i, 70.0, 2800, 30.0) for i in range(30, -1, -1)]
    db = _make_store_rich(tmp_path, events=[], frames=flat)
    chroma_a = _FakeChroma([{"ts": 100.0}])
    chroma_b = _FakeChroma([{"ts": 100.0}])
    stats_a = backfill_labels(db, chroma_a)
    stats_b = backfill_labels(db, chroma_b)
    assert stats_a == stats_b
    assert chroma_a.updates == chroma_b.updates
