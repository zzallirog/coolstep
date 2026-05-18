"""Drift-triggered Embedder re-fit + HnswStore atomic swap (P2.8+).

Pipeline
--------
  store.db (last N frames)
      │
      ▼
  Embedder.refit(frames)          ← robust median/MAD on rolling window
      │
      ▼
  holdout validation              ← last 5% of frames; cosine similarity check
      │         ╰── REJECT → keep old index, log warning
      ▼
  embed all N frames → HnswStore  ← written to staging dir
      │
      ▼
  atomic rename staging → live    ← data/hnsw.staging/ → data/hnsw/
      │
      ▼
  save embedder-stats.json + write embedder-refit.log

Safety guards:
  - Refuse if hnsw.count() < MIN_HNSW_COUNT (too sparse to refit meaningfully).
  - Skip if a predictor_spike episode is open (don't disturb live prediction).
  - Parity threshold: ≥ PARITY_THRESHOLD of hold-out vectors must stay in the
    same cosine neighbourhood; otherwise reject and keep the old index.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coolstep.core.embedding import Embedder
from coolstep.core.schema import (
    LABEL_UNKNOWN,
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    ProcSig,
    TelemetryFrame,
    WorkloadFrame,
)

if TYPE_CHECKING:
    from coolstep.adapters.storage.hnsw import HnswStore

log = logging.getLogger(__name__)

# Minimum vectors in the live index before we allow a refit.
MIN_HNSW_COUNT = 5_000
# Rolling window: number of frames pulled from store.db for the refit.
DEFAULT_WINDOW_FRAMES = 10_000
# Holdout fraction (tail of the window, by timestamp order).
HOLDOUT_FRACTION = 0.05
# Cosine similarity threshold: ε neighbourhood check.
COSINE_EPS = 0.05          # vectors within this cosine distance count as "same"
PARITY_THRESHOLD = 0.90    # fraction of holdout vectors that must pass


@dataclass
class RefitReport:
    parity_pct: float
    frames_used: int
    holdout_frames: int
    accepted: bool
    duration_ms: float
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _frame_from_jsonable(payload: dict[str, Any]) -> TelemetryFrame:
    """Deserialise a raw_json row from store.db back to TelemetryFrame."""
    cpu = CpuMetrics(**payload.get("cpu", {}))
    gpus = [GpuMetrics(**g) for g in (payload.get("gpus") or [])]
    fans = [FanMetrics(**f) for f in (payload.get("fans") or [])]
    wd_raw = payload.get("workload")
    workload: WorkloadFrame | None
    if wd_raw is None:
        workload = None
    else:
        top = [ProcSig(**p) for p in (wd_raw.get("top_processes") or [])]
        workload = WorkloadFrame(
            top_processes=top,
            rolling_features=dict(wd_raw.get("rolling_features") or {}),
            label=wd_raw.get("label"),
        )
    return TelemetryFrame(
        timestamp=float(payload["timestamp"]),
        cpu=cpu,
        gpus=gpus,
        fans=fans,
        storage_temps_c=dict(payload.get("storage_temps_c") or {}),
        memory_temps_c=dict(payload.get("memory_temps_c") or {}),
        workload=workload,
        platform_state=dict(payload.get("platform_state") or {}),
    )


def _load_recent_frames(store_path: Path, limit: int) -> list[TelemetryFrame]:
    """Read the `limit` most-recent frames from store.db as TelemetryFrames."""
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT raw_json FROM frames ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    frames: list[TelemetryFrame] = []
    errors = 0
    for (raw,) in rows:
        try:
            frames.append(_frame_from_jsonable(json.loads(raw)))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if errors <= 3:
                log.debug("embedder_refit: frame parse error: %r", exc)
    # Reverse so frames are chronological (oldest first — refit is order-stable).
    frames.reverse()
    return frames


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


def _parity_check(
    old_embedder: Embedder,
    new_embedder: Embedder,
    holdout: list[TelemetryFrame],
) -> float:
    """Return fraction of holdout frames whose new vector is within ε of old."""
    if not holdout:
        return 1.0
    matching = 0
    for frame in holdout:
        old_vec = old_embedder.embed(frame)
        new_vec = new_embedder.embed(frame)
        if old_vec is None or new_vec is None:
            continue
        sim = _cosine_sim(old_vec, new_vec)
        if (1.0 - sim) <= COSINE_EPS:
            matching += 1
    return matching / len(holdout)


def refit_and_swap(  # noqa: C901 (linear pipeline — splitting would hurt readability)
    store_path: Path,
    hnsw_store: HnswStore,
    current_embedder: Embedder,
    embedder_stats_path: Path,
    refit_log_path: Path,
    spike_active: bool = False,
    window_frames: int = DEFAULT_WINDOW_FRAMES,
) -> RefitReport:
    """Orchestrate the full refit pipeline.

    Parameters
    ----------
    store_path
        Path to the live store.db (opened read-only).
    hnsw_store
        Live HnswStore instance (used for count check and staging dir derivation).
    current_embedder
        The daemon's current Embedder (used as parity reference and replaced
        in-place on success by updating its stats from new_embedder).
    embedder_stats_path
        Where to persist the new embedder stats on success.
    refit_log_path
        Append-only JSONL file for refit reports.
    spike_active
        If True, skip the refit (predictor_spike open episode).
    window_frames
        Number of most-recent frames to use for the refit window.

    Returns
    -------
    RefitReport with outcome details.
    """
    t0 = time.monotonic()

    def _skip(reason: str) -> RefitReport:
        report = RefitReport(
            parity_pct=0.0,
            frames_used=0,
            holdout_frames=0,
            accepted=False,
            duration_ms=(time.monotonic() - t0) * 1000.0,
            skipped_reason=reason,
        )
        _append_log(refit_log_path, report)
        return report

    # Guard: predictor_spike open.
    if spike_active:
        log.info("embedder_refit: skipping — predictor_spike episode open")
        return _skip("spike_active")

    # Guard: hnsw too sparse.
    hnsw_count = hnsw_store.count()
    if hnsw_count < MIN_HNSW_COUNT:
        log.info(
            "embedder_refit: skipping — hnsw count %d < %d (need warm history)",
            hnsw_count, MIN_HNSW_COUNT,
        )
        return _skip(f"hnsw_count={hnsw_count}<{MIN_HNSW_COUNT}")

    # Load rolling window from store.db.
    frames = _load_recent_frames(store_path, window_frames)
    if len(frames) < current_embedder.min_frames_to_fit:
        log.warning("embedder_refit: not enough frames (%d)", len(frames))
        return _skip(f"frames={len(frames)}<{current_embedder.min_frames_to_fit}")

    # Split holdout (last 5%).
    holdout_n = max(1, int(len(frames) * HOLDOUT_FRACTION))
    train_frames = frames[:-holdout_n]
    holdout_frames = frames[-holdout_n:]

    # Fit new embedder on train split.
    new_embedder = Embedder(min_frames_to_fit=current_embedder.min_frames_to_fit)
    new_embedder.refit(train_frames)
    if not new_embedder.fitted:
        return _skip("new_embedder.refit failed")

    # Parity validation on holdout.
    parity = _parity_check(current_embedder, new_embedder, holdout_frames)
    log.info(
        "embedder_refit: parity=%.3f on %d holdout frames (threshold=%.2f)",
        parity, len(holdout_frames), PARITY_THRESHOLD,
    )

    if parity < PARITY_THRESHOLD:
        log.warning(
            "embedder_refit: REJECTED — parity %.3f < %.2f; keeping old index",
            parity, PARITY_THRESHOLD,
        )
        report = RefitReport(
            parity_pct=parity,
            frames_used=len(frames),
            holdout_frames=len(holdout_frames),
            accepted=False,
            duration_ms=(time.monotonic() - t0) * 1000.0,
            skipped_reason="parity_below_threshold",
        )
        _append_log(refit_log_path, report)
        return report

    # Build new HNSW index in staging dir.  Wipe any leftovers from a prior
    # crashed run — otherwise an old `meta.sqlite` survives, our INSERT OR
    # REPLACE keeps its zombie rows, and the post-swap `count()` (sqlite)
    # diverges from `_index.element_count` (HNSW), silently corrupting the
    # KNN backend.  See ADR-024 (parity gate would catch some of this; this
    # is the belt to its braces).
    import shutil
    staging_dir = hnsw_store.persist_dir.parent / "hnsw.staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        import hnswlib  # type: ignore[import-untyped]
    except ImportError:
        return _skip("hnswlib not installed")

    from coolstep.adapters.storage.hnsw import (  # noqa: PLC0415
        DEFAULT_DIM,
        DEFAULT_EF_CONSTRUCTION,
        DEFAULT_EF_QUERY,
        DEFAULT_M,
        INDEX_FILENAME,
        INITIAL_CAPACITY,
        META_FILENAME,
        _ts_to_id,
    )
    from coolstep.core.storage_common import normalise_metadata  # noqa: PLC0415

    # Build staging index.
    idx = hnswlib.Index(space="cosine", dim=DEFAULT_DIM)
    idx.init_index(
        max_elements=max(INITIAL_CAPACITY, len(frames) + 1024),
        ef_construction=DEFAULT_EF_CONSTRUCTION,
        M=DEFAULT_M,
        allow_replace_deleted=True,
    )
    idx.set_ef(DEFAULT_EF_QUERY)

    import numpy as np

    staging_db_path = staging_dir / META_FILENAME
    staging_db = sqlite3.connect(str(staging_db_path), isolation_level=None)
    staging_db.execute(
        "CREATE TABLE IF NOT EXISTS meta ("
        " id INTEGER PRIMARY KEY,"
        " ts REAL NOT NULL,"
        " meta_json TEXT NOT NULL"
        ")"
    )
    staging_db.execute("CREATE INDEX IF NOT EXISTS meta_ts ON meta(ts)")

    added = 0
    for frame in frames:
        cpu_temp_raw = (
            frame.cpu.temps_c.get("tctl")
            or frame.cpu.temps_c.get("tdie")
            or frame.cpu.temps_c.get("package")
        )
        if cpu_temp_raw is None or cpu_temp_raw <= 0.0:
            continue
        vec = new_embedder.embed(frame)
        if vec is None:
            continue
        gpu_temps = [g.temp_c for g in frame.gpus if g.temp_c is not None]
        gpu_temp = max(gpu_temps) if gpu_temps else 0.0
        fan_rpms = [f.rpm for f in frame.fans if f.rpm is not None]
        fan_max = max(fan_rpms) if fan_rpms else 0
        label = str(frame.workload.label) if (frame.workload and frame.workload.label) else ""
        meta = normalise_metadata({
            "ts": float(frame.timestamp),
            "cpu_temp_at": float(cpu_temp_raw),
            "gpu_temp_at": float(gpu_temp),
            "fan_max_at": int(fan_max),
            "workload_label": label,
            "was_hot_in_30s": LABEL_UNKNOWN,
            "peak_temp_after": -1.0,
        })
        id_ = _ts_to_id(frame.timestamp)
        arr = np.asarray(vec, dtype=np.float32).reshape(1, -1)
        idx.add_items(arr, ids=[id_], replace_deleted=True)
        with staging_db:
            staging_db.execute(
                "INSERT OR REPLACE INTO meta(id, ts, meta_json) VALUES (?, ?, ?)",
                (id_, frame.timestamp, json.dumps(meta)),
            )
        added += 1

    staging_db.close()
    idx.save_index(str(staging_dir / INDEX_FILENAME))

    # Atomic-ish swap: rename staging → live.  A SIGKILL in the 2-line
    # window between `live_dir.rename(live_backup)` and
    # `staging_dir.rename(live_dir)` would leave `live_dir` missing —
    # `HnswStore.discover` checks for `hnsw.backup/` on a missing live
    # dir and restores it before init.  Use ignore_errors on rmtree so
    # a TOCTOU-symlinked backup target can't trick us into rm'ing the
    # wrong path.
    live_dir = hnsw_store.persist_dir
    live_backup = live_dir.parent / "hnsw.backup"
    shutil.rmtree(live_backup, ignore_errors=True)
    if live_dir.exists():
        live_dir.rename(live_backup)
    staging_dir.rename(live_dir)
    log.info(
        "embedder_refit: atomic swap done — %d vectors in new index (backup: %s)",
        added, live_backup,
    )

    # Persist new embedder stats (updates current_embedder in-place).
    current_embedder._stats = new_embedder._stats
    current_embedder._fitted = True
    try:
        new_embedder.save_stats(embedder_stats_path)
    except OSError as exc:
        log.warning("embedder_refit: stats persist failed: %r", exc)

    duration_ms = (time.monotonic() - t0) * 1000.0
    report = RefitReport(
        parity_pct=parity,
        frames_used=len(frames),
        holdout_frames=len(holdout_frames),
        accepted=True,
        duration_ms=duration_ms,
    )
    log.info(
        "embedder_refit: ACCEPTED — parity=%.3f frames=%d duration=%.0fms",
        parity, len(frames), duration_ms,
    )
    _append_log(refit_log_path, report)
    return report


def _append_log(path: Path, report: RefitReport) -> None:
    """Append one JSON line to the refit audit log."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), **report.to_dict()}
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.debug("embedder_refit log write failed: %r", exc)
