#!/usr/bin/env python3
"""Warm-start reindex: store.db frames → chroma vectors.

После recovery из chroma SEGV (2026-05-12 → 2026-05-13) live архив в
store.db уцелел, но chroma пустая. Daemon наполняет chroma только из
новых tick'ов — старые 11h+ остаются dead weight для KNN retrieval.

Этот скрипт:
  1. Читает frames из sqlite store.db (READ-ONLY, без блокировки daemon).
  2. Reconstruct'ит TelemetryFrame из raw_json.
  3. Fit'ит Embedder на первых N frames (default 60), embed'ит остальные.
  4. Пишет в Chroma с was_hot_in_30s=LABEL_UNKNOWN (placeholder).
  5. Вызывает backfill_labels(store, chroma) — расставит HOT/COOL +
     danger / is_stable / equilibrium_rpm на основе throttle_events.

Idempotent: skip ts, уже присутствующих в chroma.

Pre-condition: коллектор остановлен (`systemctl --user stop
coolstep-collector.service`), иначе race на chroma writer.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coolstep.adapters.storage.chroma import ChromaStore  # noqa: E402
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


def _existing_ids(chroma: ChromaStore) -> set[str]:
    if not chroma.available:
        return set()
    coll = chroma._collection  # noqa: SLF001 — internal access OK for one-shot script
    try:
        res = coll.get(include=[], limit=10**7)
    except Exception as exc:
        print(f"warn: could not enumerate existing ids: {exc}", file=sys.stderr)
        return set()
    return set(res.get("ids") or [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", type=Path,
                    default=Path.home() / "coolstep" / "data" / "store.db")
    ap.add_argument("--fit-frames", type=int, default=60,
                    help="Frames для embedder.refit (default 60)")
    ap.add_argument("--batch-log", type=int, default=2000,
                    help="Print progress каждые N frames")
    ap.add_argument("--dry-run", action="store_true",
                    help="Парсит и embed'ит, но не пишет в chroma")
    args = ap.parse_args()

    store_path: Path = args.store
    if not store_path.exists():
        print(f"no store.db at {store_path}", file=sys.stderr)
        return 1

    chroma = ChromaStore()
    if not chroma.discover():
        print("chroma unavailable (COOLSTEP_CHROMA_DISABLED or missing)", file=sys.stderr)
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

    existing = _existing_ids(chroma)
    print(f"  {len(existing)} ids already in chroma — will skip")

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
        # Deterministic prefix fit — same sample как первый проход, чтобы
        # vectors уже в chroma остались сопоставимы с новой query'ёй после
        # того как daemon loadнет эти stats. Если хотите fit на ALL frames,
        # сначала wipe chroma collection — иначе разъедутся.
        fit_sample = frames[: args.fit_frames * 4]
        embedder.refit(fit_sample)
        if not embedder.fitted:
            print("embedder failed to fit", file=sys.stderr)
            return 4
        embedder.save_stats(stats_path)
        print(f"embedder fitted on {len(fit_sample)} frames → saved {stats_path.name}")

    skipped_no_temp = 0
    skipped_no_vec = 0
    skipped_existing = 0
    added = 0
    errors = 0
    t1 = time.monotonic()

    for frame in frames:
        fid = f"{frame.timestamp:.6f}"
        if fid in existing:
            skipped_existing += 1
            continue

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
            chroma.add(frame.timestamp, vector, meta)
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
    print(f"  added            : {added}")
    print(f"  skipped existing : {skipped_existing}")
    print(f"  skipped no_temp  : {skipped_no_temp}")
    print(f"  skipped no_vec   : {skipped_no_vec}")
    print(f"  errors           : {errors}")
    print(f"  chroma.count()   : {chroma.count()}")

    if args.dry_run:
        print("\n[dry-run] skipping backfill_labels")
        return 0

    print("\nrunning backfill_labels …")
    t2 = time.monotonic()
    stats = backfill_labels(store_path, chroma)
    print(f"  done in {time.monotonic()-t2:.1f}s")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
