#!/usr/bin/env python3
"""One-shot re-labelling of every chroma vector against current
HOT_THRESHOLD_C + LOOKAHEAD_SEC.

Use after changing the threshold, or after `claw-vm-game-mode` shifts the
operational ceiling. Reads ts → cpu_temp from sqlite frames table for the
peak lookup; updates chroma metadata only.
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coolstep.adapters.storage.chroma import ChromaStore  # noqa: E402
from coolstep.core.schema import LABEL_COOL, LABEL_HOT  # noqa: E402
from coolstep.daemon import HOT_THRESHOLD_C, LOOKAHEAD_SEC  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=HOT_THRESHOLD_C,
                    help=f"Hot threshold (°C), default {HOT_THRESHOLD_C}")
    ap.add_argument("--lookahead", type=float, default=LOOKAHEAD_SEC,
                    help=f"Lookahead window (s), default {LOOKAHEAD_SEC}")
    args = ap.parse_args()

    store_path = Path.home() / "coolstep" / "data" / "store.db"
    if not store_path.exists():
        print("no store.db", file=sys.stderr)
        return 1

    chroma = ChromaStore()
    if not chroma.discover():
        print("chroma not available", file=sys.stderr)
        return 1
    total = chroma.count()
    print(f"chroma vectors: {total}")
    if total == 0:
        return 0

    conn = sqlite3.connect(store_path)
    try:
        rows = conn.execute("SELECT ts, cpu_temp FROM frames ORDER BY ts").fetchall()
    finally:
        conn.close()
    if not rows:
        print("no frames in store")
        return 0

    print(f"frames in store: {len(rows)} (range {rows[0][0]:.0f} .. {rows[-1][0]:.0f})")

    # Frames already arrive sorted by ts (ORDER BY); split into parallel arrays so
    # bisect can index ts directly. Per-vector peak then reduces from O(M) linear
    # scan to O(log M + W) where W is the lookahead window size (~30 frames @ 1Hz).
    ts_arr = [r[0] for r in rows]
    temp_arr = [r[1] if r[1] is not None else 0.0 for r in rows]

    now = time.time()
    cutoff = now - args.lookahead          # frames newer than this can't be labelled yet
    horizon_misses = 0
    relabelled = 0
    hot = 0

    # list_unlabeled only matches LABEL_UNKNOWN; here we re-label EVERY vector
    # against a (potentially) new threshold, so we go through `.get()` directly.
    coll = chroma._collection                 # noqa: SLF001 — one-shot script
    page = 1000
    offset = 0
    while True:
        res = coll.get(limit=page, offset=offset, include=["metadatas"])
        ids = res.get("ids", [])
        metas = res.get("metadatas", [])
        if not ids:
            break
        batch_ids: list[str] = []
        batch_metas: list[dict[str, float | int]] = []
        for vec_id, meta in zip(ids, metas, strict=True):
            ts = float(meta.get("ts") or vec_id)
            if ts > cutoff:
                horizon_misses += 1
                continue
            lo = bisect.bisect_left(ts_arr, ts)
            hi = bisect.bisect_right(ts_arr, ts + args.lookahead, lo=lo)
            peak = max(temp_arr[lo:hi], default=0.0)
            label = LABEL_HOT if peak >= args.threshold else LABEL_COOL
            if label == LABEL_HOT:
                hot += 1
            batch_ids.append(vec_id)
            batch_metas.append({"was_hot_in_30s": label, "peak_temp_after": float(peak)})
        if batch_ids:
            coll.update(ids=batch_ids, metadatas=batch_metas)
            relabelled += len(batch_ids)
        offset += page
        print(f"  page @offset={offset}: relabelled={relabelled}, hot={hot}, "
              f"horizon_misses={horizon_misses}", flush=True)

    print(f"relabelled: {relabelled}")
    print(f"hot frames (peak >= {args.threshold}°C in next {args.lookahead}s): {hot}")
    print(f"horizon misses (still in lookahead window): {horizon_misses}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
