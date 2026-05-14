#!/usr/bin/env python3
"""Warm-start reindex: store.db frames → hnsw vectors.

Mirror of `reindex_chroma_from_store.py` but writes to HnswStore. Used when
the chromadb on-disk index is unreadable (mixed hnswlib version writes) and
we can't `chroma._collection.get(include=["embeddings"])` to migrate.
Re-embeds every frame from store.db through the same Embedder; output is
bit-identical to a chroma run because both backends share the same vector
math.

Steps:
  1. Read frames from sqlite store.db (READ-ONLY, no daemon block).
  2. Reconstruct TelemetryFrame from raw_json.
  3. Fit Embedder on first N (or load persisted stats if present).
  4. Add vectors to HnswStore with was_hot_in_30s=LABEL_UNKNOWN.
  5. Call backfill_labels(store, hnsw) — same as chroma path; the helper
     uses `list_unlabeled` + `update_metadata` which HnswStore implements.

Pre-condition: collector stopped. The hnsw dir at `data/hnsw/` will be
populated; rerun replays everything (HnswStore.add upserts on id collision).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coolstep.adapters.storage.hnsw import HnswStore  # noqa: E402
from coolstep.core.backfill import backfill_labels  # noqa: E402
from coolstep.core.embedding import Embedder  # noqa: E402
from coolstep.core.schema import (  # noqa: E402
    LABEL_UNKNOWN,
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    ProcSig,
    TelemetryFrame,
    WorkloadFrame,
)


def _frame_from_jsonable(payload: dict) -> TelemetryFrame:
    cpu = CpuMetrics(**payload.get("cpu", {}))
    gpus = [GpuMetrics(**g) for g in payload.get("gpus") or []]
    fans = [FanMetrics(**f) for f in payload.get("fans") or []]
    wd_raw = payload.get("workload")
    workload: WorkloadFrame | None
    if wd_raw is None:
        workload = None
    else:
        top = [ProcSig(**p) for p in wd_raw.get("top_processes") or []]
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", type=Path,
                    default=Path.home() / "coolstep" / "data" / "store.db")
    ap.add_argument("--fit-frames", type=int, default=60,
                    help="Frames for embedder.refit (default 60)")
    ap.add_argument("--batch-log", type=int, default=5000,
                    help="Print progress every N frames")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse + embed but don't write to hnsw")
    args = ap.parse_args()

    store_path: Path = args.store
    if not store_path.exists():
        print(f"no store.db at {store_path}", file=sys.stderr)
        return 1

    hnsw = HnswStore()
    if not hnsw.discover():
        print("HnswStore.discover() failed (hnswlib missing?)", file=sys.stderr)
        return 2

    print(f"reading frames from {store_path} …")
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ts, raw_json FROM frames ORDER BY ts ASC"
        ).fetchall()
    finally:
        conn.close()
    print(f"  {len(rows)} rows")

    # Parse all frames first (cheap, ~50ms / 10k frames).
    t0 = time.monotonic()
    frames: list[TelemetryFrame] = []
    parse_errors = 0
    for ts, raw in rows:
        try:
            frames.append(_frame_from_jsonable(json.loads(raw)))
        except Exception as exc:  # noqa: BLE001
            parse_errors += 1
            if parse_errors < 5:
                print(f"  parse error at ts={ts}: {exc}", file=sys.stderr)
    print(f"  parsed {len(frames)} frames ({parse_errors} errors) in {time.monotonic()-t0:.1f}s")

    if len(frames) < args.fit_frames:
        print(f"not enough frames to fit embedder (need {args.fit_frames})", file=sys.stderr)
        return 3

    embedder = Embedder(min_frames_to_fit=args.fit_frames)
    stats_path = store_path.parent / "embedder-stats.json"
    if embedder.load_stats(stats_path):
        print(f"embedder: loaded persisted stats from {stats_path.name}")
    else:
        # Deterministic prefix fit so future queries align with the daemon's
        # persisted Embedder once it loads the same stats file.
        fit_sample = frames[: args.fit_frames * 4]
        embedder.refit(fit_sample)
        if not embedder.fitted:
            print("embedder failed to fit", file=sys.stderr)
            return 4
        embedder.save_stats(stats_path)
        print(f"embedder fitted on {len(fit_sample)} frames → saved {stats_path.name}")

    skipped_no_temp = 0
    skipped_no_vec = 0
    added = 0
    errors = 0
    t1 = time.monotonic()

    for frame in frames:
        cpu_temp_raw = frame.cpu.temps_c.get("tctl") or frame.cpu.temps_c.get("tdie") \
            or frame.cpu.temps_c.get("package")
        if cpu_temp_raw is None or cpu_temp_raw <= 0.0:
            skipped_no_temp += 1
            continue

        vector = embedder.embed(frame)
        if vector is None:
            skipped_no_vec += 1
            continue

        gpu_temps = [g.temp_c for g in frame.gpus if g.temp_c is not None]
        gpu_temp = max(gpu_temps) if gpu_temps else 0.0
        fan_rpms = [f.rpm for f in frame.fans if f.rpm is not None]
        fan_max = max(fan_rpms) if fan_rpms else 0
        label = ""
        if frame.workload and frame.workload.label:
            label = str(frame.workload.label)

        meta = {
            "ts": float(frame.timestamp),
            "cpu_temp_at": float(cpu_temp_raw),
            "gpu_temp_at": float(gpu_temp),
            "fan_max_at": int(fan_max),
            "workload_label": label,
            "was_hot_in_30s": LABEL_UNKNOWN,
            "peak_temp_after": -1.0,
        }

        if args.dry_run:
            added += 1
            continue

        try:
            hnsw.add(frame.timestamp, vector, meta)
            added += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if errors < 5:
                print(f"  add err ts={frame.timestamp}: {exc}", file=sys.stderr)

        if added and added % args.batch_log == 0:
            elapsed = time.monotonic() - t1
            rate = added / elapsed if elapsed > 0 else 0
            print(f"  added {added}/{len(frames)} ({rate:.0f}/s)")

    elapsed = time.monotonic() - t1
    print(f"\nreindex done in {elapsed:.1f}s:")
    print(f"  added           : {added}")
    print(f"  skipped no_temp : {skipped_no_temp}")
    print(f"  skipped no_vec  : {skipped_no_vec}")
    print(f"  errors          : {errors}")
    print(f"  hnsw.count()    : {hnsw.count()}")

    if args.dry_run:
        print("\n[dry-run] skipping persist + backfill_labels")
        return 0

    hnsw.persist()
    print(f"  hnsw.persist()   ok ({hnsw.dir_size_bytes() // 1024}KB on disk)")

    print("\nrunning backfill_labels …")
    t2 = time.monotonic()
    stats = backfill_labels(store_path, hnsw)
    print(f"  done in {time.monotonic()-t2:.1f}s")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    hnsw.persist()
    print(f"  final persist ok ({hnsw.dir_size_bytes() // 1024}KB)")
    print(f"  hnsw.count_labeled() = {hnsw.count_labeled()} / {hnsw.count()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
