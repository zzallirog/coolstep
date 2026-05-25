"""Steady-state efficiency calibration — Phase G of P2.5.

Background analyser that walks a window of telemetry frames and asks:
*for each (workload, power, ambient) combination, what is the lowest
fan RPM that has historically held the chip in a flat thermal regime?*
The answer goes to ``data/efficiency_table.jsonl`` as an append-only
log; the dashboard's efficiency tile reads the latest-per-bucket view.

This phase only **produces** the dataset. A follow-up phase would wire
it into the curve as a policy. For now the table is observational.

Design constraints:

* Pure function ``analyse_window()`` — no I/O, no threading. The daemon
  decides when to invoke it (every N=600 ticks ≈ 10 min in the existing
  async block that hosts ``_append_drift_history`` / ``store.rotate``).
* Append-only persistence (`append_table`) so the historical view is
  preserved; consumers that want "latest-per-bucket" call ``load_table``.
* Rotation pattern mirrors :mod:`coolstep.core.incidents` — 3-gen
  rotation at 5 MB.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from coolstep.core._helpers import canonical_cpu_temp
from coolstep.core.schema import TelemetryFrame

log = logging.getLogger(__name__)

EFFICIENCY_FILE = "efficiency_table.jsonl"
# 5 MB rotation — mirrors incidents.py / decision.py.
MAX_EFFICIENCY_BYTES: int = int(
    os.environ.get("COOLSTEP_EFFICIENCY_MAX_BYTES", "5000000")
)
DISABLED_ENV = "COOLSTEP_EFFICIENCY_LOG_DISABLED"

POWER_BUCKET_W = 10
AMBIENT_BUCKET_C = 2


@dataclass(slots=True, frozen=True)
class EfficiencyRow:
    """One observed efficiency point.

    ``min_rpm_for_stable`` is the *minimum* fan_max_rpm seen in any
    stable run that landed in this (workload, power, ambient) bucket.
    Subsequent observations with a lower min supersede prior rows for
    the same key; older rows stay on disk (history is preserved).
    """

    workload_class: str
    power_bucket_w: int
    ambient_bucket_c: int
    min_rpm_for_stable: int
    sample_count: int
    last_seen_ts: float


# ── helpers (file conventions mirror incidents.py) ─────────────────────


def _efficiency_path() -> Path:
    home_env = os.environ.get("COOLSTEP_HOME")
    home = Path(home_env) if home_env else Path.home() / "coolstep" / "data"
    return home / EFFICIENCY_FILE


def _rotate(path: Path) -> None:
    """3-generation rotation matching the incidents/actuator-journal
    pattern. Best-effort — OSError is logged + swallowed."""
    gen2 = Path(str(path) + ".2")
    gen1 = Path(str(path) + ".1")
    try:
        if gen1.exists():
            os.rename(gen1, gen2)
        os.rename(path, gen1)
    except OSError as exc:
        log.debug("efficiency log rotation failed: %r", exc)


def _tctl(frame: TelemetryFrame) -> float | None:
    # G-9 — vendor-agnostic CPU temp (see store.py:write_frame for full rationale).
    return canonical_cpu_temp(frame)


def _cpu_power_w(frame: TelemetryFrame) -> float | None:
    """Package power if available, else None (k10temp won't expose it
    on Ryzen 7940HS today — caller treats None as «unknown bucket»)."""
    power = frame.cpu.power_w.get("package") or frame.cpu.power_w.get("pkg")
    if power is None:
        return None
    try:
        return float(power)
    except (TypeError, ValueError):
        return None


def _ambient_c(frame: TelemetryFrame) -> float | None:
    """Best-effort ambient. Fall back to platform_state if a collector
    surfaces it; otherwise None → bucket 0 (= unknown)."""
    ambient_str = frame.platform_state.get("ambient_c")
    if ambient_str is None:
        return None
    try:
        return float(ambient_str)
    except (TypeError, ValueError):
        return None


def _fan_max_rpm(frame: TelemetryFrame) -> int | None:
    candidates = [fan.rpm for fan in frame.fans if fan.rpm is not None]
    return max(candidates) if candidates else None


def _workload_label(frame: TelemetryFrame) -> str | None:
    return frame.workload.label if frame.workload is not None else None


def _bucket(value: float, step: int) -> int:
    """Round ``value`` to the nearest ``step``. Negative values rounded
    the same way so cold ambient (−5 °C) bucket cleanly to −6 etc."""
    return int(round(value / step)) * step


# ── analysis ────────────────────────────────────────────────────────────


def analyse_window(
    frames: Sequence[TelemetryFrame],
    *,
    min_stable_secs: float = 10.0,
    temp_slope_tolerance_c_per_s: float = 0.05,
) -> list[EfficiencyRow]:
    """Find stable runs and reduce each (workload, power, ambient) bucket
    to its minimum-RPM observation.

    A *stable run* is a contiguous subsequence of frames whose ΔTctl /
    Δt stays within ``±temp_slope_tolerance_c_per_s`` for at least
    ``min_stable_secs``. Within the run we group frames by their
    (workload_class, power_bucket_w, ambient_bucket_c) key; each
    resulting group becomes one :class:`EfficiencyRow`.

    Frames missing Tctl, fan RPM, or workload label are skipped — they
    can't anchor a bucket. Frames missing power_w fall into the 0-W
    bucket (the daemon's k10temp case); ambient missing falls into 0-°C
    bucket. Both are valid keys; the table just has «unknown» strata
    until the kernel exposes the readings.
    """
    if len(frames) < 2:
        return []

    runs = _find_stable_runs(
        frames,
        min_stable_secs=min_stable_secs,
        tolerance=temp_slope_tolerance_c_per_s,
    )
    if not runs:
        return []

    # bucket_key -> aggregator dict
    agg: dict[tuple[str, int, int], dict[str, float]] = {}
    for run in runs:
        for frame in run:
            label = _workload_label(frame)
            if not label:
                continue
            rpm = _fan_max_rpm(frame)
            if rpm is None:
                continue
            power = _cpu_power_w(frame) or 0.0
            ambient = _ambient_c(frame)
            ambient_bucket = _bucket(ambient, AMBIENT_BUCKET_C) if ambient is not None else 0
            power_bucket = _bucket(power, POWER_BUCKET_W)

            key = (label, power_bucket, ambient_bucket)
            cur = agg.get(key)
            if cur is None:
                agg[key] = {
                    "min_rpm": float(rpm),
                    "count": 1.0,
                    "last_ts": float(frame.timestamp),
                }
            else:
                if rpm < cur["min_rpm"]:
                    cur["min_rpm"] = float(rpm)
                cur["count"] += 1.0
                if frame.timestamp > cur["last_ts"]:
                    cur["last_ts"] = float(frame.timestamp)

    rows: list[EfficiencyRow] = []
    for (label, power_bucket, ambient_bucket), v in agg.items():
        rows.append(
            EfficiencyRow(
                workload_class=label,
                power_bucket_w=int(power_bucket),
                ambient_bucket_c=int(ambient_bucket),
                min_rpm_for_stable=int(v["min_rpm"]),
                sample_count=int(v["count"]),
                last_seen_ts=float(v["last_ts"]),
            )
        )
    # Stable order so logs are diffable in tests + grep
    rows.sort(key=lambda r: (r.workload_class, r.power_bucket_w, r.ambient_bucket_c))
    return rows


def _find_stable_runs(
    frames: Sequence[TelemetryFrame],
    *,
    min_stable_secs: float,
    tolerance: float,
) -> list[list[TelemetryFrame]]:
    """Scan ``frames`` in order, group consecutive frames whose Tctl
    slope (pairwise) stays inside the tolerance band, and emit runs
    whose total duration is at least ``min_stable_secs``.

    Frames lacking Tctl break the current run (we can't measure
    stability on them) but don't disqualify subsequent ones."""
    runs: list[list[TelemetryFrame]] = []
    current: list[TelemetryFrame] = []

    def _close() -> None:
        if len(current) < 2:
            current.clear()
            return
        duration = current[-1].timestamp - current[0].timestamp
        if duration >= min_stable_secs:
            runs.append(list(current))
        current.clear()

    for frame in frames:
        t = _tctl(frame)
        if t is None:
            _close()
            continue
        if not current:
            current.append(frame)
            continue
        prev_frame = current[-1]
        prev_t = _tctl(prev_frame)
        dt = frame.timestamp - prev_frame.timestamp
        if prev_t is None or dt <= 0:
            _close()
            current.append(frame)
            continue
        slope = (t - prev_t) / dt
        if abs(slope) <= tolerance:
            current.append(frame)
        else:
            _close()
            current.append(frame)
    _close()
    return runs


# ── persistence ─────────────────────────────────────────────────────────


def _row_to_json(row: EfficiencyRow) -> str:
    payload = {
        "workload_class": row.workload_class,
        "power_bucket_w": int(row.power_bucket_w),
        "ambient_bucket_c": int(row.ambient_bucket_c),
        "min_rpm_for_stable": int(row.min_rpm_for_stable),
        "sample_count": int(row.sample_count),
        "last_seen_ts": float(row.last_seen_ts),
    }
    return json.dumps(payload, separators=(",", ":"))


def _as_int(value: object) -> int:
    """Narrow ``object`` (from a json.loads dict) to int. Raises ValueError
    on anything that can't be reasonably coerced — callers wrap in
    try/except to drop the row."""
    if isinstance(value, bool):
        # bool is a subclass of int, but a true/false in this schema is
        # almost certainly corruption.
        raise ValueError("bool is not accepted as int here")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise ValueError(f"cannot coerce {type(value).__name__} to int")


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("bool is not accepted as float here")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise ValueError(f"cannot coerce {type(value).__name__} to float")


def _row_from_dict(row: dict[str, object]) -> EfficiencyRow | None:
    try:
        return EfficiencyRow(
            workload_class=str(row["workload_class"]),
            power_bucket_w=_as_int(row["power_bucket_w"]),
            ambient_bucket_c=_as_int(row["ambient_bucket_c"]),
            min_rpm_for_stable=_as_int(row["min_rpm_for_stable"]),
            sample_count=_as_int(row["sample_count"]),
            last_seen_ts=_as_float(row["last_seen_ts"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def append_table(
    rows: list[EfficiencyRow],
    path: Path | None = None,
) -> None:
    """Append ``rows`` to the efficiency table.

    Append-only — older rows for the same bucket key are not rewritten;
    :func:`load_table` does the latest-per-bucket reduction at read time.
    A row is only written if it represents a new minimum (i.e. its
    ``min_rpm_for_stable`` is strictly lower than the latest on-disk row
    for the same key) OR the bucket has never been recorded. This keeps
    the journal small without throwing away history.

    Rotation: when the file size hits ``MAX_EFFICIENCY_BYTES`` (5 MB by
    default), we rotate ``.jsonl`` → ``.jsonl.1`` → ``.jsonl.2`` and
    continue. Best-effort: OSError is swallowed.

    Tests / non-write contexts: set ``COOLSTEP_EFFICIENCY_LOG_DISABLED=1``
    to make this a no-op.
    """
    if not rows:
        return
    if os.environ.get(DISABLED_ENV, "").lower() in {"1", "true", "yes"}:
        return
    target = path or _efficiency_path()
    # Pre-compute the latest-per-bucket from disk so we can skip writes
    # that don't improve the minimum (keeps the log compact even when
    # the analyser keeps re-discovering the same plateau).
    latest = {(r.workload_class, r.power_bucket_w, r.ambient_bucket_c): r
              for r in load_table(path=target)}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        for row in rows:
            key = (row.workload_class, row.power_bucket_w, row.ambient_bucket_c)
            prev = latest.get(key)
            if prev is not None and row.min_rpm_for_stable >= prev.min_rpm_for_stable:
                continue
            if target.exists() and target.stat().st_size >= MAX_EFFICIENCY_BYTES:
                _rotate(target)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(_row_to_json(row) + "\n")
            latest[key] = row
    except OSError as exc:
        log.debug("efficiency write failed: %r", exc)


def load_table(path: Path | None = None) -> list[EfficiencyRow]:
    """Read the efficiency table and return the *latest* row per
    (workload, power, ambient) bucket.

    "Latest" here means "lowest min_rpm seen so far" — because the table
    is append-only and we only ever append improvements, the last row
    for any bucket on disk is also the row with the lowest min_rpm. If
    the on-disk ordering is ever disturbed (e.g. manual edit), we still
    pick the lowest min_rpm to stay deterministic.
    """
    target = path or _efficiency_path()
    if not target.exists():
        return []
    rows: list[EfficiencyRow] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed = _row_from_dict(raw) if isinstance(raw, dict) else None
                if parsed is not None:
                    rows.append(parsed)
    except OSError as exc:
        log.debug("efficiency read failed: %r", exc)
        return []
    # Reduce to latest-per-bucket (lowest min_rpm wins, ties broken by
    # newer last_seen_ts so the dashboard surfaces fresh data).
    best: dict[tuple[str, int, int], EfficiencyRow] = {}
    for row in rows:
        key = (row.workload_class, row.power_bucket_w, row.ambient_bucket_c)
        prev = best.get(key)
        if prev is None:
            best[key] = row
            continue
        if row.min_rpm_for_stable < prev.min_rpm_for_stable or (
            row.min_rpm_for_stable == prev.min_rpm_for_stable
            and row.last_seen_ts > prev.last_seen_ts
        ):
            best[key] = row
    out = list(best.values())
    out.sort(key=lambda r: (r.workload_class, r.power_bucket_w, r.ambient_bucket_c))
    return out


def _default_tmp_path() -> Path:
    """Convenience for callers that want a throwaway path (mostly
    tests); CLAUDE.md asks us to use ``tempfile.gettempdir()`` rather
    than hardcoded ``/tmp``."""
    return Path(tempfile.gettempdir()) / EFFICIENCY_FILE


# ── small convenience for dashboard / inspect callers ───────────────────


def latest_per_bucket(rows: Sequence[EfficiencyRow]) -> list[EfficiencyRow]:
    """Pure-function counterpart of :func:`load_table`'s reduction step.

    Useful when callers already hold an in-memory list and want the
    same «one row per bucket» view without round-tripping through disk.
    """
    best: dict[tuple[str, int, int], EfficiencyRow] = defaultdict()
    for row in rows:
        key = (row.workload_class, row.power_bucket_w, row.ambient_bucket_c)
        prev = best.get(key)
        if prev is None or row.min_rpm_for_stable < prev.min_rpm_for_stable:
            best[key] = row
    out = list(best.values())
    out.sort(key=lambda r: (r.workload_class, r.power_bucket_w, r.ambient_bucket_c))
    return out
