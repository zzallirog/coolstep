"""Actuator registry — same discover-on-import pattern as collectors."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from coolstep.adapters.actuators._base import Actuator

log = logging.getLogger(__name__)

_SKIP_MODULES = {"_base"}


def discover() -> list[Actuator]:
    out: list[Actuator] = []
    package_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(package_dir)]):
        if mod_info.name in _SKIP_MODULES or mod_info.ispkg:
            continue
        full_name = f"{__name__}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001
            log.debug("actuator %s import failed: %s", full_name, exc)
            continue
        make = getattr(module, "make", None)
        if make is None:
            continue
        try:
            actuator = make()
        except Exception as exc:  # noqa: BLE001
            log.debug("actuator %s.make() raised: %s", full_name, exc)
            continue
        if actuator is not None:
            out.append(actuator)
    return out


__all__ = ["Actuator", "discover"]
