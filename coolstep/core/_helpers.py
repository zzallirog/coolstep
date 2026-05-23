"""Shared core helpers."""

from __future__ import annotations

from coolstep.core.schema import TelemetryFrame


def canonical_cpu_temp(frame: TelemetryFrame) -> float | None:
    """Vendor-agnostic CPU temp shortcut.

    AMD primary -> AMD secondary -> Intel package -> max-of-cores universal.
    """
    return (
        frame.cpu.temps_c.get("tctl")
        or frame.cpu.temps_c.get("tdie")
        or frame.cpu.temps_c.get("package")
        or (max(frame.cpu.temps_c.values()) if frame.cpu.temps_c else None)
    )
