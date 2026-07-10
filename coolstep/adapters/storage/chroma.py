"""Isolated ChromaDB persistent client for coolstep.

NOT shared with claw-db-router — different lifecycle, different schemas, different
TTL. Lives entirely under `~/coolstep/data/chroma/` so removing coolstep removes
all its vectors with it.

Single collection `frames` — every tick adds a vector with metadata:
  - ts (timestamp, primary join key with sqlite frames table)
  - cpu_temp_at, gpu_temp_at, fan_max_at — shortcut numerics for filter queries
  - workload_label
  - was_hot_in_30s — proxy throttle label, populated by lookahead backfill once
    `now - 30 < ts`. Until then: -1 (uncapped). KNN treats -1 as 'unknown', skips.
  - was_danger_vector (P2.5-E) — 1 if a throttle landed within +30s AND the
    frame itself had a load spike (cpu_load_max jumped ≥ 70% vs prior frame).
    Default 0. Backfilled together with was_hot_in_30s.
  - is_stable (P2.5-F) — 1 if the trailing 30s window had |ΔT/Δt| < 0.05 °C/s,
    |Δrpm/Δt| < 50 RPM/s and |Δload/Δt| < 5 %/s. Default 0.
  - equilibrium_rpm (P2.5-F) — when is_stable=1, median fan_max_rpm over the
    trailing 30s window. -1.0 otherwise. Read by KnnPredictor stable-state
    query and surfaced as `Prediction.suggested_rpm`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from coolstep.core.schema import LABEL_UNKNOWN
from coolstep.core.storage_common import normalise_metadata as _normalise_metadata

log = logging.getLogger(__name__)

COLLECTION_NAME = "frames"


def _coolstep_chroma_dir() -> Path:
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / "chroma"


class ChromaStore:
    """Thin wrapper. Lazy-import chromadb so missing dep doesn't break daemon."""

    def __init__(self, persist_dir: Path | None = None) -> None:
        self.persist_dir = persist_dir or _coolstep_chroma_dir()
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client: Any = None
        self._collection: Any = None
        self._available = False

    def discover(self) -> bool:
        # P2.5 — operational kill switch. The chromadb rust bindings have
        # been segfaulting on this host since 2026-05-11; setting
        # `COOLSTEP_CHROMA_DISABLED=1` makes the daemon fall back to
        # MetaPredictor(TrajectoryBaseline) instead of crash-looping (see
        # ADR-017; daemon.py predictor wiring). Predictor degrades
        # gracefully — calibration paused, no KNN learning, but the
        # adaptive curve / mode switching / incident logger keep working.
        if os.environ.get("COOLSTEP_CHROMA_DISABLED", "").lower() in {"1", "true", "yes"}:
            log.warning("ChromaDB disabled via COOLSTEP_CHROMA_DISABLED — using fallback predictor")
            return False
        # Python 3.14 guard — README promises «daemon detects and falls back
        # automatically» for the known chromadb rust-bindings segfault on
        # 3.14 (rust.py:440 Collection._get). A segfault kills the process
        # before any except-clause, so detection must happen BEFORE import.
        # Opt back in (e.g. a fixed chromadb build) via COOLSTEP_CHROMA_FORCE=1.
        if (
            sys.version_info >= (3, 14)
            and os.environ.get("COOLSTEP_CHROMA_FORCE", "").lower() not in {"1", "true"}
        ):
            log.warning(
                "Python %d.%d: chromadb rust bindings are known to segfault on 3.14 "
                "— using fallback predictor (set COOLSTEP_CHROMA_FORCE=1 to override)",
                sys.version_info[0], sys.version_info[1],
            )
            return False
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError:
            log.warning("chromadb not installed — KNN predictor disabled")
            return False

        # P2.9.6 — silence chromadb 0.6.3 posthog telemetry wrapper.
        # The wrapper's capture() signature mismatch raises TypeError on
        # every chroma operation (ClientStart, CollectionGet, Add, Query,
        # Update); chromadb catches the exception but the per-call overhead
        # blocks daemon tick rate from 1Hz to ~0.05Hz (operator-observed
        # 3 frames/min in store.db).  ANONYMIZED_TELEMETRY=False env var
        # is ignored by 0.6.3, so we hard-noop the capture method here.
        try:
            from chromadb.telemetry.product import posthog as _ph
            _ph.Posthog.capture = lambda *_a, **_kw: None
        except (ImportError, AttributeError):
            pass
        try:
            self._client = chromadb.PersistentClient(
                path=str(self.persist_dir),
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ChromaDB init failed: %s", exc)
            return False
        self._available = True
        return True

    @property
    def available(self) -> bool:
        return self._available

    def add(self, ts: float, vector: list[float], metadata: dict[str, Any]) -> None:
        if not self._available:
            return
        try:
            self._collection.add(
                ids=[f"{ts:.6f}"],
                embeddings=[vector],
                metadatas=[_normalise_metadata(metadata)],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("chroma add failed: %s", exc)

    def query(self, vector: list[float], top_k: int = 20,
              labeled_only: bool = True) -> list[dict[str, Any]]:
        """Return list of neighbour dicts: {ts, distance, metadata}.

        labeled_only=True (default) пропускает recent unlabeled frames через
        where-фильтр — иначе KNN тонет в last-минуте однообразия.
        """
        if not self._available:
            return []
        kwargs: dict[str, Any] = dict(
            query_embeddings=[vector],
            n_results=top_k,
            include=["distances", "metadatas"],
        )
        if labeled_only:
            kwargs["where"] = {"was_hot_in_30s": {"$ne": LABEL_UNKNOWN}}
        try:
            res = self._collection.query(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.debug("chroma query failed: %s", exc)
            return []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        return [
            {"ts": float(i), "distance": float(d), "metadata": m}
            for i, d, m in zip(ids, dists, metas, strict=True)
        ]

    def query_stable(self, vector: list[float], top_k: int = 10) -> list[dict[str, Any]]:
        """KNN restricted to vectors flagged `is_stable=1`. Predictor uses
        these to derive a `suggested_rpm` (Phase F). Returns the same shape
        as `query()`; empty list when nothing stable has been labelled yet.
        """
        if not self._available:
            return []
        kwargs: dict[str, Any] = dict(
            query_embeddings=[vector],
            n_results=top_k,
            include=["distances", "metadatas"],
            where={"is_stable": 1},
        )
        try:
            res = self._collection.query(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.debug("chroma query_stable failed: %s", exc)
            return []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        return [
            {"ts": float(i), "distance": float(d), "metadata": m}
            for i, d, m in zip(ids, dists, metas, strict=True)
        ]

    def update_metadata(self, ts: float, metadata: dict[str, Any]) -> None:
        """Backfill labels (was_hot_in_30s, peak_temp_after, plus the P2.5
        danger-vector / stable-window keys) into a past frame."""
        if not self._available:
            return
        try:
            self._collection.update(
                ids=[f"{ts:.6f}"],
                metadatas=[_normalise_metadata(metadata)],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("chroma update failed: %s", exc)

    def count(self) -> int:
        if not self._available:
            return 0
        try:
            return int(self._collection.count())
        except Exception:  # noqa: BLE001
            return 0

    def count_labeled(self) -> int:
        """Vectors with `was_hot_in_30s != LABEL_UNKNOWN` (i.e. backfill done).
        Predictor warm-up indicator; dashboard shows N / min_required."""
        if not self._available:
            return 0
        try:
            # Query both 0 and 1 labels (everything except LABEL_UNKNOWN=-1)
            res = self._collection.get(
                where={"was_hot_in_30s": {"$ne": -1}},
                include=[],  # only ids, much smaller payload
            )
            return len(res.get("ids", []))
        except Exception:  # noqa: BLE001
            return 0

    def dir_size_bytes(self) -> int:
        """Total bytes used by the chroma persist dir (incident-2026-05-04 watchdog).

        Used by daemon._chroma_size_guard to detect runaway link_lists.bin growth
        before it eats the disk. See docs/incident-2026-05-04-chroma-bloat.md.
        """
        total = 0
        try:
            for p in self.persist_dir.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        except OSError:
            return total
        return total

    def list_stable(self, limit: int = 10_000) -> list[dict[str, Any]]:
        """Return frames flagged `is_stable=1`. Read-only consumer for the
        cluster-drift analyser. Empty list if chroma unavailable or no rows.
        """
        if not self._available:
            return []
        try:
            res = self._collection.get(
                where={"is_stable": 1},
                limit=limit,
                include=["metadatas"],
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("chroma list_stable failed: %s", exc)
            return []
        ids = res.get("ids", [])
        metas = res.get("metadatas", [])
        return [
            {"ts": float(i), "metadata": m}
            for i, m in zip(ids, metas, strict=True)
        ]

    def list_unlabeled(self, before_ts: float, limit: int = 200) -> list[dict[str, Any]]:
        """Return frames whose `was_hot_in_30s == -1` and ts < before_ts."""
        if not self._available:
            return []
        try:
            res = self._collection.get(
                where={
                    "$and": [
                        {"was_hot_in_30s": LABEL_UNKNOWN},
                        {"ts": {"$lt": before_ts}},
                    ]
                },
                limit=limit,
                include=["metadatas"],
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("chroma list_unlabeled failed: %s", exc)
            return []
        ids = res.get("ids", [])
        metas = res.get("metadatas", [])
        return [
            {"ts": float(i), "metadata": m}
            for i, m in zip(ids, metas, strict=True)
        ]


# Metadata coercion (was inline here, lifted into coolstep/core/storage_common.py
# so HnswStore shares the exact same key-type table — drift between the two
# backends was a real risk during the P2.5 label additions).
