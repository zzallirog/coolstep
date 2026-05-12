"""Backfill `was_hot_in_30s` labels on chroma vectors from sqlite throttle_events.

Why this exists
---------------
The live daemon labels chroma vectors via two paths:

  1. `_backfill_labels()` — peeks at the `_labelled_window` buffer (recent
     frames still in-memory) and decides HOT/COOL from the +30s temperature
     trajectory using `HOT_THRESHOLD_C` (82°C).
  2. `_recover_orphan_labels()` — at startup, marks anything still
     LABEL_UNKNOWN that's older than 60s as LABEL_COOL (the buffer is gone).

Both miss this case: a real throttle episode happened (`throttle_events` row
exists), but the +30s buffer either rolled off (long uptime gap) or the
labelled-window window-peak landed under 82°C while the FSM still entered
"hot" via the 90/85 hysteresis. The lookahead temperatures and the FSM look
at different signals.

This module re-labels every `LABEL_UNKNOWN` chroma vector by **trusting the
FSM** (= the `throttle_events` table) as ground truth:

  - any vector whose ts falls in `(event.ts_start - 30, event.ts_start]` →
    LABEL_HOT (peak_temp_after = event.peak_temp)
  - everything else → LABEL_COOL (peak_temp_after = max cpu_temp in the
    vector's [ts, ts+30s] window from `frames`; 0.0 if no frames).

Idempotent: re-running with the same store + chroma produces the same labels.

Adapter note
------------
`ChromaStore.list_unlabeled(before_ts, limit=200)` exists, but the agreed
spec calls it with no args. We adapt — pass `before_ts = +inf` (now + 1d to
cover any clock skew) and `limit = 100_000` to surface everything still
LABEL_UNKNOWN at backfill time. If the adapter ever drops support, the
`hasattr` probe falls through to an empty list (no-op).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from coolstep.core.schema import LABEL_COOL, LABEL_HOT

log = logging.getLogger(__name__)

LOOKAHEAD_SEC = 30.0  # match daemon.LOOKAHEAD_SEC
# Trailing stable-window for is_stable / equilibrium_rpm detection.
STABLE_WINDOW_SEC = 30.0
# Slope tolerances (Phase F spec, end-vs-start delta over the window).
STABLE_TEMP_SLOPE_MAX_C_S = 0.05   # °C / s
STABLE_RPM_SLOPE_MAX_RPM_S = 50.0  # RPM / s
STABLE_LOAD_SLOPE_MAX_PCT_S = 5.0  # % / s
# Spike threshold for danger-vector label (Phase E). cpu_load_max is the
# proxy for power on this APU (k10temp does not expose RAPL). 70 pp jump
# between two adjacent samples = workload going from idle to flat-out in
# under a second.
DANGER_LOAD_JUMP_PCT = 70.0
# Cap on how many unlabeled vectors we try to fetch in one pass. 100k is
# well above the realistic ceiling (~14d * 86400s / period = ~1.2M frames at
# 1s period, but chroma is capped to fewer via rotation in practice).
_UNLABELED_LIMIT = 100_000


def _load_events(store_path: Path) -> list[tuple[float, float]] | None:
    """Pull (ts_start, peak_temp) rows ordered by ts_start. None on error."""
    try:
        conn = sqlite3.connect(store_path)
        try:
            rows = conn.execute(
                "SELECT ts_start, peak_temp FROM throttle_events "
                "ORDER BY ts_start"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("backfill: failed to read throttle_events: %r", exc)
        return None
    return [(float(r[0]), float(r[1])) for r in rows]


def _fetch_unlabeled(chroma: Any) -> list | None:
    """Probe the chroma adapter for unlabeled vectors. None on failure."""
    if not hasattr(chroma, "list_unlabeled"):
        log.info("backfill: chroma has no list_unlabeled, skipping")
        return None
    try:
        return chroma.list_unlabeled(
            before_ts=time.time() + 86400.0,
            limit=_UNLABELED_LIMIT,
        )
    except TypeError:
        # Older adapter shape — fall back to argless call.
        try:
            return chroma.list_unlabeled()
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill: list_unlabeled failed: %r", exc)
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill: list_unlabeled failed: %r", exc)
        return None


def _event_peak_in_window(
    ts: float, events: list[tuple[float, float]],
) -> float | None:
    """Return max event peak_temp for events in (ts, ts+LOOKAHEAD_SEC].
    None if no event matches."""
    peak: float | None = None
    for ev_ts, ev_peak in events:
        if ev_ts <= ts:
            continue
        if ev_ts > ts + LOOKAHEAD_SEC:
            break  # events sorted by ts_start, no more in window
        if peak is None or ev_peak > peak:
            peak = ev_peak
    return peak


def _frame_peak_in_window(store_path: Path, ts: float) -> float:
    """Best-effort max cpu_temp in [ts, ts+LOOKAHEAD_SEC] from frames table."""
    try:
        conn = sqlite3.connect(store_path)
        try:
            row = conn.execute(
                "SELECT MAX(cpu_temp) FROM frames WHERE ts >= ? AND ts <= ?",
                (ts, ts + LOOKAHEAD_SEC),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0
    if row and row[0] is not None:
        return float(row[0])
    return 0.0


def _load_max_from_raw(raw_json: str | None) -> float | None:
    """Extract max(cpu.load_pct) from a frames.raw_json blob. None if absent."""
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    cpu = data.get("cpu") if isinstance(data, dict) else None
    if not isinstance(cpu, dict):
        return None
    loads = cpu.get("load_pct")
    if not isinstance(loads, list) or not loads:
        return None
    try:
        return max(float(x) for x in loads)
    except (TypeError, ValueError):
        return None


def _detect_spike(store_path: Path, ts: float) -> bool:
    """True iff cpu_load_max jumped ≥ DANGER_LOAD_JUMP_PCT vs the previous
    frame in `frames` (looking back up to ~3s for the most-recent sample).

    Reads raw_json for the two latest frames at-or-before ts. If load data is
    missing on either side, we conservatively say False (no spike).
    """
    try:
        conn = sqlite3.connect(store_path)
        try:
            rows = conn.execute(
                "SELECT ts, raw_json FROM frames "
                "WHERE ts <= ? AND ts > ? "
                "ORDER BY ts DESC LIMIT 2",
                (ts, ts - 5.0),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if len(rows) < 2:
        return False
    cur_load = _load_max_from_raw(rows[0][1])
    prev_load = _load_max_from_raw(rows[1][1])
    if cur_load is None or prev_load is None:
        return False
    return (cur_load - prev_load) >= DANGER_LOAD_JUMP_PCT


def _stable_window_metrics(
    store_path: Path, ts: float,
) -> tuple[bool, float]:
    """Inspect the trailing STABLE_WINDOW_SEC seconds for the (is_stable,
    equilibrium_rpm) labels.

    Slopes are end-vs-start deltas over the window (simpler than OLS and
    fine for «is it sitting still» detection). Returns (False, -1.0) if the
    window has fewer than 2 samples or any slope exceeds its tolerance.
    """
    try:
        conn = sqlite3.connect(store_path)
        try:
            rows = conn.execute(
                "SELECT ts, cpu_temp, fan_max_rpm, raw_json FROM frames "
                "WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                (ts - STABLE_WINDOW_SEC, ts),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return False, -1.0
    if len(rows) < 2:
        return False, -1.0
    duration = float(rows[-1][0]) - float(rows[0][0])
    if duration <= 0.0:
        return False, -1.0

    temps = [r[1] for r in rows if r[1] is not None]
    rpms = [r[2] for r in rows if r[2] is not None]
    loads = []
    for r in rows:
        lm = _load_max_from_raw(r[3])
        if lm is not None:
            loads.append(lm)
    # Need both endpoints of every series for an end-vs-start delta.
    if len(temps) < 2 or len(rpms) < 2 or len(loads) < 2:
        return False, -1.0

    temp_slope = (float(temps[-1]) - float(temps[0])) / duration
    rpm_slope = (float(rpms[-1]) - float(rpms[0])) / duration
    load_slope = (loads[-1] - loads[0]) / duration

    if abs(temp_slope) >= STABLE_TEMP_SLOPE_MAX_C_S:
        return False, -1.0
    if abs(rpm_slope) >= STABLE_RPM_SLOPE_MAX_RPM_S:
        return False, -1.0
    if abs(load_slope) >= STABLE_LOAD_SLOPE_MAX_PCT_S:
        return False, -1.0

    return True, float(statistics.median(float(r) for r in rpms))


def _label_one(
    entry: dict,
    events: list[tuple[float, float]],
    store_path: Path,
    chroma: Any,
    stats: dict,
) -> None:
    """Classify + push metadata for one chroma vector. Updates stats in place.

    Writes (in a single update_metadata call so partial state can't appear):
      - was_hot_in_30s, peak_temp_after — original P1 lookahead labels.
      - was_danger_vector — 1 iff was_hot=HOT AND a cpu_load_max jump of
        ≥ DANGER_LOAD_JUMP_PCT vs the previous frame is observable in
        frames.raw_json. 0 otherwise. P2.5-E.
      - is_stable + equilibrium_rpm — stable-window detector over the
        trailing STABLE_WINDOW_SEC. equilibrium_rpm = median(fan_max_rpm)
        when stable, -1.0 otherwise. P2.5-F.

    Idempotent: re-running on the same frames table produces the same labels.
    """
    try:
        ts = float(entry.get("ts", 0.0))
    except (TypeError, ValueError):
        stats["errors"] += 1
        return
    if ts <= 0.0:
        stats["errors"] += 1
        return

    event_peak = _event_peak_in_window(ts, events)
    if event_peak is not None:
        was_hot = LABEL_HOT
        peak_after = event_peak
    else:
        was_hot = LABEL_COOL
        peak_after = _frame_peak_in_window(store_path, ts)

    danger = 0
    if was_hot == LABEL_HOT and _detect_spike(store_path, ts):
        danger = 1

    stable, equilibrium_rpm = _stable_window_metrics(store_path, ts)

    try:
        chroma.update_metadata(ts, {
            "was_hot_in_30s": was_hot,
            "peak_temp_after": peak_after,
            "was_danger_vector": danger,
            "is_stable": 1 if stable else 0,
            "equilibrium_rpm": equilibrium_rpm,
        })
    except Exception as exc:  # noqa: BLE001
        log.debug("backfill: update_metadata failed for ts=%.2f: %r", ts, exc)
        stats["errors"] += 1
        return

    if was_hot == LABEL_HOT:
        stats["labeled_hot"] += 1
    else:
        stats["labeled_cool"] += 1
    if danger:
        stats["danger_vectors"] += 1
    if stable:
        stats["stable_vectors"] += 1


def backfill_labels(store_path: Path, chroma: Any) -> dict:
    """Re-label `LABEL_UNKNOWN` chroma vectors using `throttle_events` table.

    For each unlabeled vector V at ts:
      - If ANY throttle_events row has ts_start in (V.ts, V.ts + 30s]:
            V.was_hot_in_30s = LABEL_HOT (1)
            V.peak_temp_after = the matching event's peak_temp (max if many)
      - Else:
            V.was_hot_in_30s = LABEL_COOL (0)
            V.peak_temp_after = max cpu_temp in V's [ts, ts+30s] window from
                the `frames` table (best-effort; 0.0 if no frames).

    Idempotent — running twice yields the same labels (assumes the chroma
    adapter's `update_metadata` is idempotent, which it is on overwrite).
    Best-effort: errors per vector are logged at DEBUG and skipped; the loop
    continues. Adapter-level errors short-circuit and return stats so far.

    Returns:
        dict with keys: ``unlabeled_before``, ``labeled_hot``, ``labeled_cool``,
        ``errors``.
    """
    stats = {
        "unlabeled_before": 0,
        "labeled_hot": 0,
        "labeled_cool": 0,
        "danger_vectors": 0,
        "stable_vectors": 0,
        "errors": 0,
    }
    if not Path(store_path).exists():
        log.info("backfill: store missing, skipping")
        return stats
    if not getattr(chroma, "available", False):
        log.info("backfill: chroma unavailable, skipping")
        return stats

    events = _load_events(store_path)
    if events is None:
        return stats

    unlabeled = _fetch_unlabeled(chroma)
    if unlabeled is None:
        return stats
    stats["unlabeled_before"] = len(unlabeled)
    if not unlabeled:
        log.info("backfill: 0 unlabeled vectors, nothing to do")
        return stats

    for entry in unlabeled:
        _label_one(entry, events, store_path, chroma, stats)

    log.info(
        "backfill: %d unlabeled -> %d hot + %d cool + %d errors "
        "(%d danger, %d stable)",
        stats["unlabeled_before"],
        stats["labeled_hot"],
        stats["labeled_cool"],
        stats["errors"],
        stats["danger_vectors"],
        stats["stable_vectors"],
    )
    return stats
