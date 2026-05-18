"""Two-layer storage: snapshot archive + drift detector — P2.5 Heavy-4.

A *snapshot* is a frozen «golden state» of the system at a known-good
moment — fresh after cleaning the fan, fresh after a calibration pass,
or a manual marker the user wanted to keep («the chip ran cool today,
remember this»). It captures three things:

1. The Embedder's normalisation stats (median + MAD per feature) — the
   axes the predictor was learning on at that moment.
2. Per-profile median temperatures (idle / load60 / load100) — the
   physical truth: «at this scene, the chip sat at X °C».
3. A sample count + free-text note for traceability.

Drift between today and a baseline snapshot is computed as the simple
Celsius delta per profile: ``+5 °C at load60 vs the post-cleaning
baseline`` is a hard signal that the heatsink is gunked, or paste has
aged, or ambient has shifted. The drift detector here is intentionally
dumb — no statistics, just a delta — because the *baseline choice* is
the cognitive work and that lives with the user.

Storage layer mirrors ``incidents.py``: append-only jsonl at
``data/snapshot_archive.jsonl``, 3-generation rotation at 5 MB. We do
not import the helper from ``incidents.py`` on purpose — keeping the
two journals decoupled means a refactor of incidents rotation won't
silently change snapshot behaviour.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOTS_FILE = "snapshot_archive.jsonl"
# Same 5 MB rotation as incidents/actuator-journal; keeps the audit
# semantics consistent across the codebase.
MAX_SNAPSHOTS_BYTES: int = int(
    os.environ.get("COOLSTEP_SNAPSHOTS_MAX_BYTES", "5000000")
)
# Mirrors `COOLSTEP_INCIDENTS_LOG_DISABLED` / `COOLSTEP_DECISIONS_LOG_DISABLED`
# — let tests disable real writes when poking data dirs is undesirable.
DISABLED_ENV = "COOLSTEP_SNAPSHOTS_LOG_DISABLED"

# Reasons that justify creating a snapshot. Free-form strings (we store
# whatever the caller sends) but these are the well-known values UIs
# should expect.
REASON_MANUAL = "manual"
REASON_POST_CLEANING = "post_cleaning"
REASON_CALIBRATION_PASS = "calibration_pass"


def _snapshots_path() -> Path:
    home = Path(
        os.environ.get(
            "COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")
        )
    )
    return home / SNAPSHOTS_FILE


def _rotate(path: Path) -> None:
    """3-generation rotation copied (not imported) from incidents.py.
    Best-effort: any OSError is logged + dropped, never raised. We
    intentionally duplicate the helper so a refactor of incidents
    rotation won't silently change snapshot behaviour."""
    gen2 = Path(str(path) + ".2")
    gen1 = Path(str(path) + ".1")
    try:
        if gen1.exists():
            os.rename(gen1, gen2)
        os.rename(path, gen1)
    except OSError as exc:
        log.debug("snapshot archive rotation failed: %r", exc)


@dataclass(slots=True, frozen=True)
class Snapshot:
    """One frozen golden-state record.

    `median_embedder_stats` is the dict returned by
    `Embedder.stats_snapshot()` — feature_name → {median, mad}.
    The three `median_*_temp_c` floats are scene-conditional medians
    over the sampling window — see `compute_drift` for how they're
    compared.
    """

    ts: float
    reason: str  # "manual" | "post_cleaning" | "calibration_pass"
    median_embedder_stats: dict[str, dict[str, float]]
    median_idle_temp_c: float
    median_load60_temp_c: float
    median_load100_temp_c: float
    sample_n: int
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": float(self.ts),
            "reason": str(self.reason),
            "median_embedder_stats": {
                k: {kk: float(vv) for kk, vv in v.items()}
                for k, v in self.median_embedder_stats.items()
            },
            "median_idle_temp_c": float(self.median_idle_temp_c),
            "median_load60_temp_c": float(self.median_load60_temp_c),
            "median_load100_temp_c": float(self.median_load100_temp_c),
            "sample_n": int(self.sample_n),
            "note": str(self.note),
        }

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> Snapshot:
        stats_raw = row.get("median_embedder_stats") or {}
        # Defensive: cast everything through float so a hand-edited
        # jsonl line doesn't poison a downstream caller.
        stats: dict[str, dict[str, float]] = {}
        if isinstance(stats_raw, dict):
            for k, v in stats_raw.items():
                if isinstance(v, dict):
                    stats[str(k)] = {
                        str(kk): float(vv) for kk, vv in v.items()
                    }
        return cls(
            ts=float(row.get("ts") or 0.0),
            reason=str(row.get("reason") or ""),
            median_embedder_stats=stats,
            median_idle_temp_c=float(row.get("median_idle_temp_c") or 0.0),
            median_load60_temp_c=float(row.get("median_load60_temp_c") or 0.0),
            median_load100_temp_c=float(row.get("median_load100_temp_c") or 0.0),
            sample_n=int(row.get("sample_n") or 0),
            note=str(row.get("note") or ""),
        )


def write_snapshot(snap: Snapshot, *, path: Path | None = None) -> None:
    """Append-only at data/snapshot_archive.jsonl.

    Best-effort — OSError at debug, never raised. The caller (a
    «snapshot now» button, a calibration-pass hook) shouldn't fail
    because we couldn't write a log line. Tests disable writes via
    ``COOLSTEP_SNAPSHOTS_LOG_DISABLED=1``."""
    if os.environ.get(DISABLED_ENV, "").lower() in {"1", "true", "yes"}:
        return
    target = path or _snapshots_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size >= MAX_SNAPSHOTS_BYTES:
            _rotate(target)
        line = json.dumps(snap.to_dict(), separators=(",", ":"))
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        log.debug("snapshot write failed: %r", exc)


def read_snapshots(*, path: Path | None = None) -> list[Snapshot]:
    """Load every snapshot from the jsonl archive, newest-first.

    Used by the drift tile + by callers picking a baseline to diff
    against. Returns an empty list if the file is absent or all rows
    are malformed."""
    target = path or _snapshots_path()
    if not target.exists():
        return []
    rows: list[Snapshot] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                rows.append(Snapshot.from_dict(row))
    except OSError as exc:
        log.debug("snapshot read failed: %r", exc)
        return []
    rows.sort(key=lambda s: s.ts, reverse=True)
    return rows


def compute_drift(current: Snapshot, baseline: Snapshot) -> dict[str, float]:
    """Return {kind: delta_celsius} per profile (idle/load60/load100).

    Positive delta = chip running hotter in the same scene → degradation
    signal (gunked heatsink, aged paste, hotter ambient). Negative delta
    = chip running cooler than baseline → a *good* drift, e.g. after a
    cleaning pass when the baseline is pre-cleaning. The drift tile
    renders these directly; sign is the user's job to interpret.
    """
    return {
        "idle": float(current.median_idle_temp_c) - float(baseline.median_idle_temp_c),
        "load60": float(current.median_load60_temp_c) - float(baseline.median_load60_temp_c),
        "load100": float(current.median_load100_temp_c) - float(baseline.median_load100_temp_c),
    }
