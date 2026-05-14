"""Shared schema-coercion helpers for the KNN stores.

`ChromaStore` (adapters/storage/chroma.py) and `HnswStore` (core/knn_hnsw.py)
both persist metadata that chromadb rejects unless coerced to primitive
int/float/str. Keeping the type-key tables in one place avoids drift when
new label keys are added.
"""

from __future__ import annotations

from typing import Any

INT_KEYS = ("was_hot_in_30s", "was_danger_vector", "is_stable", "fan_max_at")
FLOAT_KEYS = (
    "ts",
    "cpu_temp_at",
    "gpu_temp_at",
    "peak_temp_after",
    "equilibrium_rpm",
)


def normalise_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in meta.items():
        if val is None:
            continue
        if key in INT_KEYS:
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                continue
        elif key in FLOAT_KEYS:
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                continue
        else:
            out[key] = val
    return out
