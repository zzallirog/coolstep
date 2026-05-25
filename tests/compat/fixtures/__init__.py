"""Synthetic hardware snapshots for coolstep.compat tests.

Each `<name>/snapshot.py` declares a complete platform: CPU, GPU, hwmon,
distro, modules, env, binaries on PATH, expected detect_caps() output.

The `_loader.fake_platform(snapshot)` ctx-manager materialises the snapshot
into tmp_path and patches detect.py's Path constants + helpers, so that
`detect_caps()` reads the synthetic tree end-to-end.

This is the "all OSes / all CPUs / all sensors" emulation harness — instead
of a VM farm, we replay real-world /sys layouts captured (or hand-built from
upstream docs) for representative hardware. Bug-case overlays in
`../bugcases/` mutate base snapshots to reproduce concrete gotchas.
"""

from tests.compat.fixtures._loader import (
    Snapshot,
    apply_overlay,
    fake_platform,
    list_snapshots,
    load_snapshot,
)

__all__ = [
    "Snapshot",
    "apply_overlay",
    "fake_platform",
    "list_snapshots",
    "load_snapshot",
]
