"""Backend selector for the KNN store. Defaults to ChromaStore; flip to
HnswStore via `COOLSTEP_KNN_BACKEND=hnsw`. See ADR-022 in
`docs/stack-decisions.md` for the rationale and dep pin notes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, get_args

log = logging.getLogger(__name__)

Backend = Literal["chroma", "hnsw"]
_VALID: tuple[Backend, ...] = get_args(Backend)


def resolve_backend(explicit: str | None = None) -> Backend:
    name = (explicit or os.environ.get("COOLSTEP_KNN_BACKEND", "chroma")).strip().lower()
    if name in _VALID:
        return name
    log.warning("Unknown COOLSTEP_KNN_BACKEND=%r — falling back to chroma", name)
    return "chroma"


def make_knn_store(backend: str | None = None, **kwargs: Any) -> Any:
    """Construct the configured KNN store. Same surface as ChromaStore."""
    name = resolve_backend(backend)
    if name == "hnsw":
        from coolstep.adapters.storage.hnsw import HnswStore
        return HnswStore(**kwargs)
    from coolstep.adapters.storage.chroma import ChromaStore
    return ChromaStore(**kwargs)
