"""Tests for embedder_refit pipeline and DriftGate.should_refit()."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from coolstep.core.cluster_drift import DriftGate
from coolstep.core.embedder_refit import (
    PARITY_THRESHOLD,
    refit_and_swap,
)
from coolstep.core.embedding import Embedder
from coolstep.core.schema import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    TelemetryFrame,
    WorkloadFrame,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DAY = 24 * 3600.0
NOW = 1_000_000.0
HOUR = 3600.0


def _make_frame(ts: float, temp: float = 65.0) -> TelemetryFrame:
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(
            temps_c={"tctl": temp},
            freq_mhz=[3000.0, 3100.0],
            load_pct=[20.0, 25.0],
            power_w={"package": 15.0},
        ),
        gpus=[GpuMetrics(name="gpu0", temp_c=55.0, power_w=20.0, sclk_mhz=1500.0, util_pct=30.0)],
        fans=[FanMetrics(name="cpu_fan", rpm=1200)],
        storage_temps_c={},
        memory_temps_c={},
        workload=WorkloadFrame(
            top_processes=[],
            rolling_features={"visible_apps": 2.0, "unique_classes": 1.0},
            label="code",
        ),
        platform_state={"epp": "balance_power", "boost": "1"},
    )


def _frame_to_jsonable(frame: TelemetryFrame) -> dict[str, Any]:
    """Inverse of _frame_from_jsonable — for building synthetic store.db."""
    d = asdict(frame)
    d["timestamp"] = frame.timestamp
    return d


def _make_store_db(path: Path, frames: list[TelemetryFrame]) -> None:
    """Populate a minimal store.db with raw_json rows."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS frames ("
        " ts REAL PRIMARY KEY,"
        " cpu_temp REAL,"
        " raw_json TEXT NOT NULL"
        ")"
    )
    for f in frames:
        raw = json.dumps(_frame_to_jsonable(f))
        conn.execute(
            "INSERT OR REPLACE INTO frames(ts, cpu_temp, raw_json) VALUES (?, ?, ?)",
            (f.timestamp, f.cpu.temps_c.get("tctl", 0.0), raw),
        )
    conn.commit()
    conn.close()


class _FakeHnswStore:
    """Minimal HnswStore duck-type for refit tests."""

    def __init__(self, persist_dir: Path, count_val: int = 10_000) -> None:
        self.persist_dir = persist_dir
        self._count = count_val

    def count(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# DriftGate tests
# ---------------------------------------------------------------------------


def test_should_refit_false_on_single_drift_event():
    gate = DriftGate(min_consecutive=3, min_gap_sec=HOUR)
    gate.record({"code": 5.0}, now=NOW)
    assert not gate.should_refit()


def test_should_refit_false_without_min_gap():
    """Three events within <1h: only the first is counted (the others refresh
    the timestamp in-place), so streak stays at 1."""
    gate = DriftGate(min_consecutive=3, min_gap_sec=HOUR)
    gate.record({"code": 5.0}, now=NOW)
    gate.record({"code": 5.0}, now=NOW + 60)     # < 1h gap → in-place update
    gate.record({"code": 5.0}, now=NOW + 120)    # < 1h gap → in-place update
    assert gate.streak_len == 1
    assert not gate.should_refit()


def test_should_refit_true_after_three_consecutive_events_spaced_1h():
    gate = DriftGate(min_consecutive=3, min_gap_sec=HOUR)
    gate.record({"code": 5.0}, now=NOW)
    gate.record({"code": 5.0}, now=NOW + HOUR + 1)
    gate.record({"code": 5.0}, now=NOW + 2 * HOUR + 2)
    assert gate.streak_len == 3
    assert gate.should_refit()


def test_should_refit_resets_on_empty_drift_map():
    gate = DriftGate(min_consecutive=3, min_gap_sec=HOUR)
    gate.record({"code": 5.0}, now=NOW)
    gate.record({"code": 5.0}, now=NOW + HOUR + 1)
    # Empty map → streak reset
    gate.record({}, now=NOW + 2 * HOUR + 2)
    assert gate.streak_len == 0
    assert not gate.should_refit()


def test_drift_gate_reset_clears_streak():
    gate = DriftGate(min_consecutive=2, min_gap_sec=HOUR)
    gate.record({"code": 5.0}, now=NOW)
    gate.record({"code": 5.0}, now=NOW + HOUR + 1)
    assert gate.should_refit()
    gate.reset()
    assert not gate.should_refit()
    assert gate.streak_len == 0


# ---------------------------------------------------------------------------
# refit_and_swap tests
# ---------------------------------------------------------------------------


def _build_synthetic_world(tmp_path: Path, n_frames: int = 1000) -> tuple[
    Path, _FakeHnswStore, Embedder, Path, Path
]:
    """Return (store_path, hnsw_store, embedder, stats_path, log_path)."""
    frames = [_make_frame(float(i), temp=60.0 + (i % 10) * 0.5) for i in range(n_frames)]

    store_path = tmp_path / "store.db"
    _make_store_db(store_path, frames)

    hnsw_dir = tmp_path / "hnsw"
    hnsw_dir.mkdir()
    hnsw_store = _FakeHnswStore(persist_dir=hnsw_dir, count_val=10_000)

    embedder = Embedder(min_frames_to_fit=60)
    embedder.refit(frames)

    stats_path = tmp_path / "embedder-stats.json"
    embedder.save_stats(stats_path)
    refit_log = tmp_path / "embedder-refit.log"
    return store_path, hnsw_store, embedder, stats_path, refit_log


def test_refit_and_swap_skips_when_spike_active(tmp_path: Path):
    store_path, hnsw, embedder, stats_path, log_path = _build_synthetic_world(tmp_path)
    report = refit_and_swap(
        store_path=store_path,
        hnsw_store=hnsw,  # type: ignore[arg-type]
        current_embedder=embedder,
        embedder_stats_path=stats_path,
        refit_log_path=log_path,
        spike_active=True,
    )
    assert not report.accepted
    assert report.skipped_reason == "spike_active"


def test_refit_and_swap_skips_when_hnsw_too_sparse(tmp_path: Path):
    store_path, _, embedder, stats_path, log_path = _build_synthetic_world(tmp_path)
    sparse_hnsw = _FakeHnswStore(persist_dir=tmp_path / "hnsw", count_val=100)
    report = refit_and_swap(
        store_path=store_path,
        hnsw_store=sparse_hnsw,  # type: ignore[arg-type]
        current_embedder=embedder,
        embedder_stats_path=stats_path,
        refit_log_path=log_path,
        spike_active=False,
    )
    assert not report.accepted
    assert "hnsw_count" in report.skipped_reason


def test_refit_and_swap_accepts_and_builds_staging(tmp_path: Path):
    """1000-frame synthetic store → refit should be accepted with parity > 0.9."""
    pytest.importorskip("hnswlib")
    store_path, hnsw, embedder, stats_path, log_path = _build_synthetic_world(
        tmp_path, n_frames=1000
    )

    report = refit_and_swap(
        store_path=store_path,
        hnsw_store=hnsw,  # type: ignore[arg-type]
        current_embedder=embedder,
        embedder_stats_path=stats_path,
        refit_log_path=log_path,
        spike_active=False,
    )

    assert report.accepted, f"expected accepted, got: {report}"
    assert report.parity_pct >= PARITY_THRESHOLD
    assert report.frames_used > 0
    assert report.holdout_frames > 0
    assert report.duration_ms > 0

    # Verify audit log was written.
    assert log_path.exists()
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    assert lines
    assert lines[-1]["accepted"] is True


def test_refit_and_swap_lands_index_on_disk(tmp_path: Path):
    """After an accepted refit the live HNSW dir must actually contain
    index.bin + meta.sqlite. The previous test only checked report.accepted
    — if the atomic rename silently no-ops the daemon would keep running
    on a stale index with no signal. Regression guard for that gap.
    """
    pytest.importorskip("hnswlib")
    store_path, hnsw, embedder, stats_path, log_path = _build_synthetic_world(
        tmp_path, n_frames=1000
    )
    live_dir = hnsw.persist_dir

    report = refit_and_swap(
        store_path=store_path,
        hnsw_store=hnsw,  # type: ignore[arg-type]
        current_embedder=embedder,
        embedder_stats_path=stats_path,
        refit_log_path=log_path,
        spike_active=False,
    )

    assert report.accepted, f"expected accepted, got {report}"
    assert (live_dir / "index.bin").exists(), "live HNSW index.bin missing post-refit"
    assert (live_dir / "meta.sqlite").exists(), "live meta.sqlite missing post-refit"
    # Backup of the old live dir should also be there (until next refit).
    assert (live_dir.parent / "hnsw.backup").exists()


def test_refit_and_swap_wipes_stale_staging(tmp_path: Path):
    """A prior refit that crashed between staging-mkdir and the final rename
    would leave `data/hnsw.staging/` with old contents. The new refit must
    NOT pick up those zombie rows — it must wipe staging first. Otherwise
    `count()` (sqlite) and `_index.element_count` (HNSW) silently diverge.
    """
    pytest.importorskip("hnswlib")
    store_path, hnsw, embedder, stats_path, log_path = _build_synthetic_world(
        tmp_path, n_frames=1000
    )
    staging_dir = hnsw.persist_dir.parent / "hnsw.staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "zombie.txt").write_text("crashed previous run leftover")

    report = refit_and_swap(
        store_path=store_path,
        hnsw_store=hnsw,  # type: ignore[arg-type]
        current_embedder=embedder,
        embedder_stats_path=stats_path,
        refit_log_path=log_path,
        spike_active=False,
    )

    assert report.accepted
    # After successful refit, staging is renamed to live, so the zombie file
    # is gone — it neither survives in staging (cleaned at start) nor leaks
    # into the new live dir.
    assert not (hnsw.persist_dir / "zombie.txt").exists()


def test_refit_and_swap_rejects_when_parity_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When parity check fails, the old index must be kept (no swap)."""
    pytest.importorskip("hnswlib")

    import coolstep.core.embedder_refit as er_mod

    # Force parity to 0.0 regardless of vectors.
    monkeypatch.setattr(er_mod, "_parity_check", lambda *_a, **_kw: 0.0)

    store_path, hnsw, embedder, stats_path, log_path = _build_synthetic_world(
        tmp_path, n_frames=1000
    )
    report = refit_and_swap(
        store_path=store_path,
        hnsw_store=hnsw,  # type: ignore[arg-type]
        current_embedder=embedder,
        embedder_stats_path=stats_path,
        refit_log_path=log_path,
        spike_active=False,
    )

    assert not report.accepted
    assert report.parity_pct == pytest.approx(0.0)
    assert report.skipped_reason == "parity_below_threshold"

    # Verify log was written.
    assert log_path.exists()
    last = json.loads(log_path.read_text().splitlines()[-1])
    assert last["accepted"] is False
