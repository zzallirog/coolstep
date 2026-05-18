"""Three-layer manifest loader — core.json + community.json + custom.json.

Layered config topology:
    L0 (core)       — bundled with package, read-only, "the genome"
    L1 (community)  — managed updates, "/etc/coolstep/community.json"
    L2 (custom)     — user overrides, "~/.config/coolstep/custom.json"

Deep-merge: dicts merge recursively, lists are replaced by default.
Special directives in any layer:
    {"_disable": true}     — remove this key from final merged result
    [{"id": "X", "_remove": true}, ...] — for community_pointers, removes
                             an item by id (compose-not-replace for lists-of-dicts)

L0 missing → loud failure (daemon won't start).
L1/L2 missing or corrupt → warning in log, skip layer.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from importlib.resources import files
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Manifest file resolution priority — first match wins per layer.
_L1_PATHS = (
    Path("/etc/coolstep/community.json"),
    Path("/usr/share/coolstep/community.json"),
)


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(raw) if raw else (Path.home() / ".config")


def _l2_paths() -> tuple[Path, ...]:
    return (
        _xdg_config_home() / "coolstep" / "custom.json",
        Path("/etc/coolstep/custom.json"),
    )


# ---------------------------------------------------------------------------
# Loading


def _load_core() -> dict[str, Any]:
    """Read the bundled core.json shipped inside the coolstep.compat package."""
    try:
        raw = (files("coolstep.compat") / "core.json").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            f"coolstep.compat.core.json missing — broken installation: {exc!r}"
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"coolstep.compat.core.json is not valid JSON: {exc!r}"
        ) from exc


def _try_load_optional(path: Path, layer_name: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("manifest L%s at %s could not be loaded: %r — skipping",
                    layer_name, path, exc)
        return None


def _find_layer(candidates: tuple[Path, ...], layer_name: str) -> dict[str, Any] | None:
    for p in candidates:
        data = _try_load_optional(p, layer_name)
        if data is not None:
            log.info("manifest L%s loaded from %s", layer_name, p)
            return data
    return None


# ---------------------------------------------------------------------------
# Deep merge


def deep_merge(base: Any, overlay: Any) -> Any:
    """Recursive merge of `overlay` onto `base`.

    Rules:
    - Both dicts → recurse per key.
    - Overlay dict has `{"_disable": true}` at any leaf → result drops the key.
    - List of dicts with `id` field — overlay entries with matching id deep-merge
      onto the base entry; `_remove: true` strips the item.
    - Anything else: overlay replaces base.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        return _merge_dict(base, overlay)
    if (
        isinstance(base, list) and isinstance(overlay, list)
        and base and isinstance(base[0], dict) and "id" in base[0]
    ):
        return _merge_id_list(base, overlay)
    return copy.deepcopy(overlay)


def _merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {k: copy.deepcopy(v) for k, v in base.items()}
    for key, val in overlay.items():
        if isinstance(val, dict) and val.get("_disable") is True:
            result.pop(key, None)
            continue
        if key in result:
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _merge_id_list(base: list[dict[str, Any]], overlay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List-of-dicts deep-merge by `id` field.

    Output ordering: id-less entries from `base` are preserved first in their
    original relative order, then overlay's id-less entries appended, then
    id-keyed entries (deep-merged) in insertion order from `base` followed by
    new ids from `overlay`.  This means the contract for community_pointers
    callers is: don't rely on absolute index, look up by `id`.
    """
    by_id: dict[str, dict[str, Any]] = {
        item["id"]: copy.deepcopy(item) for item in base if "id" in item
    }
    extras: list[dict[str, Any]] = [item for item in base if "id" not in item]

    for item in overlay:
        item_id = item.get("id")
        if item_id is None:
            extras.append(copy.deepcopy(item))
            continue
        if item.get("_remove") is True:
            by_id.pop(item_id, None)
            continue
        if item_id in by_id:
            by_id[item_id] = deep_merge(by_id[item_id], item)
        else:
            by_id[item_id] = copy.deepcopy(item)

    return extras + list(by_id.values())


# ---------------------------------------------------------------------------
# Singleton

_manifest: dict[str, Any] | None = None
_manifest_meta: dict[str, str | None] = {"l0": None, "l1": None, "l2": None}


def manifest_paths() -> dict[str, str | None]:
    """Return where each layer was loaded from (or None if absent).

    Used by `coolstep compat --manifest-paths` CLI to expose the chain.
    """
    # Force load so the metadata is populated.
    load_manifest()
    return dict(_manifest_meta)


def load_manifest() -> dict[str, Any]:
    """Return the deep-merged manifest (L0 ∪ L1 ∪ L2)."""
    global _manifest, _manifest_meta
    if _manifest is not None:
        return _manifest

    core = _load_core()
    _manifest_meta["l0"] = str(files("coolstep.compat") / "core.json")

    merged = core
    # L1
    l1 = _find_layer(_L1_PATHS, "1")
    if l1 is not None:
        merged = deep_merge(merged, l1)
        for p in _L1_PATHS:
            if p.exists():
                _manifest_meta["l1"] = str(p)
                break
    # L2
    l2 = _find_layer(_l2_paths(), "2")
    if l2 is not None:
        merged = deep_merge(merged, l2)
        for p in _l2_paths():
            if p.exists():
                _manifest_meta["l2"] = str(p)
                break

    _manifest = merged
    return _manifest


def reset_manifest(value: dict[str, Any] | None = None) -> None:
    """Replace cached manifest.  Pass None to force re-load on next call.

    Used in tests:
        reset_manifest({...})   # inject a fake manifest
        reset_manifest()        # clear cache; next load_manifest() re-reads
    """
    global _manifest, _manifest_meta
    _manifest = value
    _manifest_meta = {"l0": None, "l1": None, "l2": None}


__all__ = [
    "deep_merge",
    "load_manifest",
    "manifest_paths",
    "reset_manifest",
]
