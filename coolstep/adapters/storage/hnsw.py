"""HNSW-backed KNN store — drop-in replacement for ChromaStore on the hot path.

Sibling of `chroma.py`; same public surface so both backends are interchangeable
behind the `coolstep.core.knn.make_knn_store` selector. See ADR-022 in
`docs/stack-decisions.md` for the rationale.

Why this exists: chromadb 0.6.3 PersistentClient.query takes ~9.5s with 42k
vectors at 26 dims on this host; hnswlib stays under 1ms. The label predicate
(`was_hot_in_30s != UNKNOWN`, `is_stable == 1`) lives in the sqlite side-table
because hnswlib has no metadata filter.

ID encoding: chroma uses `f"{ts:.6f}"`; hnswlib needs `int`. Pack to integer
microseconds (`round(ts * 1e6)`); inverse divides on read. Unix-epoch ts fits
comfortably in int64.

Persistence is lazy — callers invoke `persist()` (daemon: periodic flush;
migration script: once at end). The index grows via `resize_index` when
`add_items` would overflow `get_max_elements()`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np

from coolstep.core.embedding import FEATURE_NAMES
from coolstep.core.schema import LABEL_UNKNOWN
from coolstep.core.storage_common import normalise_metadata

log = logging.getLogger(__name__)

DEFAULT_DIM = len(FEATURE_NAMES)  # stays in sync with embedding schema
DEFAULT_M = 16
DEFAULT_EF_CONSTRUCTION = 200
DEFAULT_EF_QUERY = 64
INITIAL_CAPACITY = 50_000
META_FILENAME = "meta.sqlite"
INDEX_FILENAME = "index.bin"

# Wide nets because the label backfill lags the index — without them queries
# on a fresh reindex would exhaust the candidate pool before `top_k` filtered
# results saturate.
_FETCH_MULT_LABELED = 30
_FETCH_MULT_STABLE = 10

_Predicate = Callable[[dict[str, Any]], bool]


def _ts_to_id(ts: float) -> int:
    return int(round(ts * 1_000_000))


def _id_to_ts(id_: int) -> float:
    return id_ / 1_000_000.0


def _coolstep_hnsw_dir() -> Path:
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / "hnsw"


def _keep_labeled(meta: dict[str, Any]) -> bool:
    return int(meta.get("was_hot_in_30s", LABEL_UNKNOWN)) != LABEL_UNKNOWN


def _keep_any(_meta: dict[str, Any]) -> bool:
    return True


def _keep_stable(meta: dict[str, Any]) -> bool:
    return int(meta.get("is_stable", 0)) == 1


class HnswStore:
    def __init__(
        self,
        persist_dir: Path | None = None,
        dim: int = DEFAULT_DIM,
        initial_capacity: int = INITIAL_CAPACITY,
    ) -> None:
        self.persist_dir = persist_dir or _coolstep_hnsw_dir()
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self._initial_capacity = initial_capacity
        self._index: Any = None
        self._db: sqlite3.Connection | None = None
        self._available = False
        self._lock = RLock()

    def discover(self) -> bool:
        try:
            import hnswlib  # type: ignore[import-untyped]
        except ImportError:
            log.warning("hnswlib not installed — HnswStore disabled")
            return False
        index_path = self.persist_dir / INDEX_FILENAME
        # Crash-recovery: `embedder_refit.refit_and_swap` does a 2-step
        # rename (live → live.backup → staging → live).  A SIGKILL in
        # the window between steps leaves no `live/index.bin` but a
        # populated `live.backup/`.  Restore it before init so we don't
        # silently start with an empty index and lose 42k vectors of
        # KNN history.  See ADR-024.
        if not index_path.exists():
            backup_dir = self.persist_dir.parent / "hnsw.backup"
            backup_index = backup_dir / INDEX_FILENAME
            if backup_index.exists():
                import shutil
                log.warning(
                    "HnswStore: live index missing but %s present — restoring "
                    "from backup (likely crash mid-refit)", backup_dir,
                )
                self.persist_dir.mkdir(parents=True, exist_ok=True)
                for src in backup_dir.iterdir():
                    shutil.copy2(src, self.persist_dir / src.name)
        try:
            self._index = hnswlib.Index(space="cosine", dim=self.dim)
            if index_path.exists():
                self._index.load_index(
                    str(index_path),
                    max_elements=self._initial_capacity,
                    allow_replace_deleted=True,
                )
            else:
                self._index.init_index(
                    max_elements=self._initial_capacity,
                    ef_construction=DEFAULT_EF_CONSTRUCTION,
                    M=DEFAULT_M,
                    allow_replace_deleted=True,
                )
            self._index.set_ef(DEFAULT_EF_QUERY)
            self._db = sqlite3.connect(
                self.persist_dir / META_FILENAME,
                check_same_thread=False,
                isolation_level=None,
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                " id INTEGER PRIMARY KEY,"
                " ts REAL NOT NULL,"
                " meta_json TEXT NOT NULL"
                ")"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS meta_ts ON meta(ts)")
            self._available = True
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("HnswStore init failed: %s", exc)
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_capacity(self) -> None:
        if self._index is None:
            return
        cap = self._index.get_max_elements()
        if self._index.element_count >= cap:
            self._index.resize_index(max(cap * 2, self._index.element_count + 1024))

    def add(self, ts: float, vector: list[float], metadata: dict[str, Any]) -> None:
        if not self._available or self._index is None or self._db is None:
            return
        meta = normalise_metadata(metadata)
        id_ = _ts_to_id(ts)
        vec = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        with self._lock:
            try:
                self._ensure_capacity()
                # replace_deleted=True covers both fresh ids and re-adds of an
                # existing one — verified against hnswlib 0.8.0.
                self._index.add_items(vec, ids=[id_], replace_deleted=True)
                with self._db:
                    self._db.execute(
                        "INSERT OR REPLACE INTO meta(id, ts, meta_json) VALUES (?, ?, ?)",
                        (id_, ts, json.dumps(meta)),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("hnsw add failed: %s", exc)

    def query(
        self, vector: list[float], top_k: int = 20, labeled_only: bool = True,
    ) -> list[dict[str, Any]]:
        keep = _keep_labeled if labeled_only else _keep_any
        fetch_mult = _FETCH_MULT_LABELED if labeled_only else 1
        return self._query(vector, top_k, fetch_mult, keep)

    def query_stable(self, vector: list[float], top_k: int = 10) -> list[dict[str, Any]]:
        return self._query(vector, top_k, fetch_mult=_FETCH_MULT_STABLE, keep=_keep_stable)

    def _query(
        self,
        vector: list[float],
        top_k: int,
        fetch_mult: int,
        keep: _Predicate,
    ) -> list[dict[str, Any]]:
        if not self._available or self._index is None or self._db is None:
            return []
        if self._index.element_count == 0:
            return []
        vec = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        fetch_k = min(top_k * fetch_mult, self._index.element_count)
        with self._lock:
            try:
                labels, dists = self._index.knn_query(vec, k=fetch_k)
            except Exception as exc:  # noqa: BLE001
                log.debug("hnsw query failed: %s", exc)
                return []
            ids = [int(i) for i in labels[0]]
            rows = self._fetch_meta(ids)
            out: list[dict[str, Any]] = []
            for id_, distance in zip(ids, dists[0], strict=True):
                meta = rows.get(id_)
                if meta is None or not keep(meta):
                    continue
                out.append(
                    {"ts": _id_to_ts(id_), "distance": float(distance), "metadata": meta}
                )
                if len(out) >= top_k:
                    break
            return out

    def update_metadata(self, ts: float, metadata: dict[str, Any]) -> None:
        if not self._available or self._db is None:
            return
        id_ = _ts_to_id(ts)
        with self._lock:
            cur = self._db.execute("SELECT meta_json FROM meta WHERE id = ?", (id_,))
            row = cur.fetchone()
            if row is None:
                return
            try:
                existing = json.loads(row[0])
            except json.JSONDecodeError:
                existing = {}
            existing.update(normalise_metadata(metadata))
            with self._db:
                self._db.execute(
                    "UPDATE meta SET meta_json = ? WHERE id = ?",
                    (json.dumps(existing), id_),
                )

    def count(self) -> int:
        if not self._available or self._db is None:
            return 0
        # sqlite3 connection objects are not safe for concurrent execute
        # calls from multiple threads (even with check_same_thread=False —
        # that flag disables the safety check, not the underlying contention).
        # Hold the same RLock writers use so reads can't see a half-committed
        # row set or hit `close()` mid-fetch.
        with self._lock:
            if self._db is None:
                return 0
            cur = self._db.execute("SELECT COUNT(1) FROM meta")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def count_labeled(self) -> int:
        if not self._available or self._db is None:
            return 0
        with self._lock:
            if self._db is None:
                return 0
            cur = self._db.execute(
                "SELECT COUNT(1) FROM meta WHERE json_extract(meta_json, '$.was_hot_in_30s') != ?",
                (LABEL_UNKNOWN,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def dir_size_bytes(self) -> int:
        total = 0
        try:
            for p in self.persist_dir.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        except OSError:
            return total
        return total

    def list_stable(self, limit: int = 10_000) -> list[dict[str, Any]]:
        if not self._available or self._db is None:
            return []
        with self._lock:
            if self._db is None:
                return []
            cur = self._db.execute(
                "SELECT ts, meta_json FROM meta"
                " WHERE json_extract(meta_json, '$.is_stable') = 1"
                " LIMIT ?",
                (limit,),
            )
            return [
                {"ts": float(ts), "metadata": json.loads(mj)}
                for ts, mj in cur.fetchall()
            ]

    def list_unlabeled(self, before_ts: float, limit: int = 200) -> list[dict[str, Any]]:
        if not self._available or self._db is None:
            return []
        with self._lock:
            if self._db is None:
                return []
            cur = self._db.execute(
                "SELECT ts, meta_json FROM meta"
                " WHERE json_extract(meta_json, '$.was_hot_in_30s') = ?"
                " AND ts < ?"
                " LIMIT ?",
                (LABEL_UNKNOWN, before_ts, limit),
            )
            return [
                {"ts": float(ts), "metadata": json.loads(mj)}
                for ts, mj in cur.fetchall()
            ]

    def persist(self) -> None:
        if not self._available or self._index is None:
            return
        with self._lock:
            self._index.save_index(str(self.persist_dir / INDEX_FILENAME))

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._index = None
            self._available = False

    def _fetch_meta(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if self._db is None or not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        cur = self._db.execute(
            f"SELECT id, meta_json FROM meta WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        )
        out: dict[int, dict[str, Any]] = {}
        for id_, mj in cur.fetchall():
            try:
                out[int(id_)] = json.loads(mj)
            except json.JSONDecodeError:
                continue
        return out
