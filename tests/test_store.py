"""Store (sqlite) tests with tmp_path."""

from __future__ import annotations

import time

from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    ProcSig,
    TelemetryFrame,
    WorkloadFrame,
)
from coolstep.core.store import FRAMES_TTL_SEC, Store


def _make_frame(ts: float, *, tctl: float = 70.0, label: str | None = None) -> TelemetryFrame:
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(
            freq_mhz=[3000.0, 3000.0],
            load_pct=[10.0, 12.0],
            temps_c={"tctl": tctl},
            power_w={"package": 25.0},
        ),
        gpus=[GpuMetrics(name="dgpu", temp_c=45.0, power_w=20.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=2900)],
        workload=WorkloadFrame(
            top_processes=[ProcSig(pid=1, name="proc", cpu_pct=20.0, rss_mb=100.0)],
            label=label,
        ),
    )


def test_store_creates_schema(tmp_path):
    store = Store(tmp_path / "store.db")
    assert store.count_frames() == 0
    assert store.count_throttle_events() == 0
    assert store.coverage_seconds() == 0.0
    store.close()


def test_store_write_frame_round_trips(tmp_path):
    store = Store(tmp_path / "store.db")
    store.write_frame(_make_frame(100.0, tctl=80.0, label="build"))
    assert store.count_frames() == 1
    latest = store.latest_frame_row()
    assert latest is not None
    assert latest["ts"] == 100.0
    assert latest["cpu_temp"] == 80.0
    assert latest["workload_label"] == "build"
    store.close()


def test_store_coverage_seconds(tmp_path):
    store = Store(tmp_path / "store.db")
    store.write_frame(_make_frame(100.0))
    store.write_frame(_make_frame(160.0))
    assert store.coverage_seconds() == 60.0
    store.close()


def test_store_rotate_drops_old_frames(tmp_path):
    store = Store(tmp_path / "store.db")
    now = time.time()
    store.write_frame(_make_frame(now - FRAMES_TTL_SEC - 100))  # стары
    store.write_frame(_make_frame(now))  # свеж
    deleted_frames, deleted_events = store.rotate(now=now)
    assert deleted_frames == 1
    assert deleted_events == 0
    assert store.count_frames() == 1
    store.close()


def test_store_throttle_events(tmp_path):
    store = Store(tmp_path / "store.db")
    store.write_throttle_event(
        ts_start=100.0,
        ts_end=105.0,
        peak_temp=92.0,
        cause_label="cpu_thermal",
        workload_at_start="build",
    )
    assert store.count_throttle_events() == 1
    store.close()


def test_store_idempotent_insert_on_same_ts(tmp_path):
    store = Store(tmp_path / "store.db")
    store.write_frame(_make_frame(100.0, tctl=70.0))
    store.write_frame(_make_frame(100.0, tctl=80.0))
    assert store.count_frames() == 1
    latest = store.latest_frame_row()
    assert latest is not None
    assert latest["cpu_temp"] == 80.0
    store.close()


def test_store_sets_wal_autocheckpoint(tmp_path):
    """`__init__` bumps wal_autocheckpoint to 2000 pages (~8MB)."""
    store = Store(tmp_path / "store.db")
    row = store._conn.execute("PRAGMA wal_autocheckpoint").fetchone()
    assert row is not None
    assert int(row[0]) == 2000
    store.close()


def test_store_rotate_triggers_passive_checkpoint(tmp_path):
    """`rotate()` runs a PASSIVE wal_checkpoint without raising."""
    store = Store(tmp_path / "store.db")
    now = time.time()
    # A tiny DB with one fresh + one stale row exercises the DELETE +
    # checkpoint codepath end-to-end.
    store.write_frame(_make_frame(now - FRAMES_TTL_SEC - 100))
    store.write_frame(_make_frame(now))
    deleted_frames, _ = store.rotate(now=now)
    assert deleted_frames == 1
    # After rotate the DB is still readable; the PASSIVE checkpoint
    # either flushed or skipped, but did not corrupt state.
    assert store.count_frames() == 1
    store.close()


def test_store_vacuum_returns_nonnegative_and_keeps_db_readable(tmp_path):
    """`vacuum()` returns bytes_freed ≥ 0 and the DB remains queryable."""
    store = Store(tmp_path / "store.db")
    now = time.time()
    # Write a handful of frames then drop most to create freed pages.
    for i in range(64):
        store.write_frame(_make_frame(now - FRAMES_TTL_SEC - 100 - i))
    store.write_frame(_make_frame(now))
    store.rotate(now=now)
    freed = store.vacuum()
    assert freed >= 0
    # DB still queryable after VACUUM.
    assert store.count_frames() == 1
    latest = store.latest_frame_row()
    assert latest is not None
    assert latest["ts"] == now
    store.close()
