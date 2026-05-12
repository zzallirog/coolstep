"""G-9 guard — vendor-agnostic CPU temp shortcut.

Before this fix, `Store.write_frame()` and `efficiency_calibration._tctl()`
hard-coded the AMD Ryzen sensor names (`tctl`, `tdie`).  On an Intel host
where `coretemp` emits `temps_c["package"]` (from label "Package id 0"),
the shortcut returned None — sqlite `cpu_temp` column was NULL, dashboard
read null and rendered N/A, even though the real value was right there.

See `docs/aur-publishing.md` § G-9 for the case study.
"""

from __future__ import annotations

from coolstep.core.efficiency_calibration import _tctl
from coolstep.core.schema import CpuMetrics, TelemetryFrame


def _frame(temps_c: dict[str, float]) -> TelemetryFrame:
    return TelemetryFrame(timestamp=0.0, cpu=CpuMetrics(temps_c=temps_c))


# ------------------------------------------------------------ efficiency hook

def test_tctl_picks_amd_tctl_when_present() -> None:
    assert _tctl(_frame({"tctl": 72.5, "tdie": 70.0, "core 0": 71.0})) == 72.5


def test_tctl_falls_back_to_amd_tdie() -> None:
    assert _tctl(_frame({"tdie": 68.0, "core 0": 65.0})) == 68.0


def test_tctl_falls_back_to_intel_package() -> None:
    """G-9 — Intel coretemp emits 'package' (from 'Package id 0' label)."""
    assert _tctl(_frame({"package": 49.0, "core 0": 48.0})) == 49.0


def test_tctl_falls_back_to_max_of_cores() -> None:
    """Universal fallback when neither tctl/tdie/package present
    (theoretical platform — keeps the shortcut total)."""
    assert _tctl(_frame({"core 0": 60.0, "core 1": 65.0, "core 2": 62.0})) == 65.0


def test_tctl_returns_none_on_empty_temps() -> None:
    assert _tctl(_frame({})) is None


# ------------------------------------------------------------ sqlite shortcut

def test_store_cpu_temp_column_picks_intel_package(tmp_path) -> None:
    """G-9 — repeat the chain at the sqlite-write path."""
    from coolstep.core.store import Store

    store = Store(path=tmp_path / "frames.db")
    frame = _frame({"package": 49.0, "core 0": 48.0, "core 1": 47.0})
    store.write_frame(frame)

    row = store._conn.execute(
        "SELECT cpu_temp FROM frames ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == 49.0, (
        "G-9 regression — Store.write_frame() did not pick Intel 'package' "
        f"as cpu_temp shortcut.  Wrote: {row[0]!r}"
    )
