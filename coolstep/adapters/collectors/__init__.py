"""Collector registry — discover-on-import.

Each adapter module exports `make() -> Collector | None`. The registry tries to
import every sibling module and calls `make()`; modules that return None or
raise during import are silently skipped (their hardware/SDK is absent on this
host).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from coolstep.adapters.collectors._base import Collector

log = logging.getLogger(__name__)

_SKIP_MODULES = {"_base"}


def discover() -> list[Collector]:
    collectors: list[Collector] = []
    package_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(package_dir)]):
        if mod_info.name in _SKIP_MODULES or mod_info.ispkg:
            continue
        full_name = f"{__name__}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001
            log.debug("collector module %s failed to import: %s", full_name, exc)
            continue
        make = getattr(module, "make", None)
        if make is None:
            continue
        try:
            collector = make()
        except Exception as exc:  # noqa: BLE001
            log.debug("collector %s.make() raised: %s", full_name, exc)
            continue
        if collector is not None:
            collectors.append(collector)
    return collectors


__all__ = ["Collector", "discover"]
