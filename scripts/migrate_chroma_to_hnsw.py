#!/usr/bin/env python3
"""Bootstrap the HNSW index from existing chroma vectors. One-shot, idempotent.

First-pass migration tool. See ADR-022 in `docs/stack-decisions.md` for
context; prefer `reindex_hnsw_from_store.py` when chromadb's on-disk HNSW
file is unreadable (mixed `hnswlib` version writes).

Reads `data/chroma/` (must be a working chroma persist dir — i.e. unset
`COOLSTEP_CHROMA_DISABLED` for the run), writes `data/hnsw/index.bin` +
`data/hnsw/meta.sqlite`. Rerunning replays everything; per-id upserts
overwrite in place via `HnswStore.add`, so the index stays consistent.

Pre-condition: stop the collector first (`systemctl --user stop
coolstep-collector.service`), or chromadb 0.6.3 SEGV under concurrent
access. Reading is fine; writing during the run isn't.

Typical throughput: 300-500 vectors/sec; a 40k-vector chroma collection
migrates in under two minutes on a modern laptop CPU.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coolstep.adapters.storage.chroma import ChromaStore  # noqa: E402
from coolstep.adapters.storage.hnsw import HnswStore  # noqa: E402


def _iter_chroma_pages(chroma: ChromaStore, batch: int):
    coll = chroma._collection  # noqa: SLF001 — one-shot script
    offset = 0
    while True:
        page = coll.get(
            limit=batch,
            offset=offset,
            include=["embeddings", "metadatas"],
        )
        ids = page.get("ids") or []
        if not ids:
            return
        embs_raw = page.get("embeddings")
        embs = embs_raw if embs_raw is not None else []
        metas_raw = page.get("metadatas")
        metas = metas_raw if metas_raw is not None else [{}] * len(ids)
        yield ids, embs, metas
        if len(ids) < batch:
            return
        offset += len(ids)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=int, default=500, help="rows per chroma page")
    ap.add_argument("--dim", type=int, default=26, help="embedding dimension")
    ap.add_argument(
        "--capacity",
        type=int,
        default=50_000,
        help="initial hnsw capacity (auto-grows if exceeded)",
    )
    args = ap.parse_args()

    src = ChromaStore()
    if not src.discover():
        print("chroma unavailable (is COOLSTEP_CHROMA_DISABLED set?)", file=sys.stderr)
        return 2
    dst = HnswStore(dim=args.dim, initial_capacity=args.capacity)
    if not dst.discover():
        print("hnsw init failed (is hnswlib installed?)", file=sys.stderr)
        return 2

    total = src.count()
    print(f"migrating {total} vectors from chroma → hnsw (batch={args.batch})")
    started = time.monotonic()
    seen = 0
    for ids, embs, metas in _iter_chroma_pages(src, args.batch):
        for id_str, vec, meta in zip(ids, embs, metas, strict=True):
            try:
                ts = float(id_str)
            except ValueError:
                continue
            dst.add(ts, list(vec) if vec is not None else [], meta or {})
        seen += len(ids)
        elapsed = max(time.monotonic() - started, 1e-6)
        print(f"  {seen}/{total} ({seen / elapsed:.0f}/s)")

    dst.persist()
    elapsed = time.monotonic() - started
    print(
        f"done. {seen} vectors in {elapsed:.1f}s "
        f"({seen / max(elapsed, 1e-6):.0f}/s, sink={dst.persist_dir})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
