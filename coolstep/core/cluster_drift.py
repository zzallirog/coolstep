"""Cluster drift — degradation detector over stable-window vectors (P2.5-F+).

Idea (user-proposed, 2026-05-12)
--------------------------------
The chip's «same scene» behaviour should stay constant across days. If for a
given `workload_class` (Code / Render / Game / …) the mean `cpu_temp_max`
during *stable* moments is creeping up week-over-week, something physical is
degrading — paste pump-out, dust in the radiator, fan bearing fade. The
predictor will never warn about this because it's a slow-drift signal, but
it's exactly the kind of thing a background tile should surface.

This module is **read-only**. It walks the chroma store's `is_stable=1`
vectors, groups them by workload class, and compares:

  - **recent**   :: ts >= now - 24h   (newest day)
  - **trailing** :: now - window_days <= ts < now - 24h   (prior baseline)

For each workload class with samples in both buckets, we emit:

    drift_celsius = mean(recent.cpu_temp_at) - mean(trailing.cpu_temp_at)

A positive drift = chip is running hotter for the same scene → degradation.
Zero or negative drift = either improvement (cleaning) or noise.

Consumed by a future dashboard tile / background task. The daemon does NOT
call this in P2.5 — too slow for the hot loop.

DriftGate (added P2.8+)
-----------------------
Wraps `detect_cluster_drift` and tracks consecutive drift detections over
time. `should_refit()` returns True only when drift has been observed for
at least `min_consecutive` analyses spaced at least `min_gap_sec` apart.
This prevents transient noise from triggering a full embedder refit.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

RECENT_WINDOW_SEC = 24 * 3600.0
DEFAULT_WINDOW_DAYS = 7
MIN_SAMPLES_PER_BUCKET = 5  # below this we skip the class — too noisy

# DriftGate defaults — configurable via constructor.
DEFAULT_MIN_CONSECUTIVE = 3
DEFAULT_MIN_GAP_SEC = 3600.0  # 1 hour between analyses


def detect_cluster_drift(
    chroma_store: Any,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: float | None = None,
) -> dict[str, float]:
    """Return per-workload-class drift (°C) for stable vectors in the window.

    Parameters
    ----------
    chroma_store
        Object exposing `list_stable(limit)` → list of `{ts, metadata}`
        dicts. `ChromaStore` provides this. Anything duck-compatible works.
    window_days
        Total lookback (recent + trailing). The recent bucket is always the
        last 24h; the trailing bucket is the prior `window_days - 1` days.
        Must be ≥ 2 for the comparison to be meaningful.
    now
        Override for the «current» timestamp. Defaults to `time.time()`.

    Returns
    -------
    dict[str, float]
        Mapping `workload_class → drift_celsius`. Positive = scene running
        hotter than its trailing baseline. Workload classes lacking enough
        samples in either bucket are silently skipped.
    """
    if window_days < 2:
        return {}

    list_stable = getattr(chroma_store, "list_stable", None)
    if list_stable is None:
        log.debug("cluster_drift: store has no list_stable, returning {}")
        return {}

    now_ts = now if now is not None else time.time()
    floor_ts = now_ts - window_days * 24 * 3600.0
    recent_floor = now_ts - RECENT_WINDOW_SEC

    try:
        rows = list_stable(limit=100_000)
    except Exception as exc:  # noqa: BLE001
        log.warning("cluster_drift: list_stable failed: %r", exc)
        return {}

    recent: dict[str, list[float]] = defaultdict(list)
    trailing: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        ts = float(row.get("ts", 0.0))
        if ts < floor_ts or ts > now_ts:
            continue
        meta = row.get("metadata") or {}
        # `workload_class` is the canonical field (see daemon._chroma_write
        # writes `workload_label`; the upcoming workload_profile resolver
        # introduces `workload_class`). Accept either for forward compat.
        cls = meta.get("workload_class") or meta.get("workload_label")
        if not cls:
            continue
        cls_str = str(cls)
        temp_raw = meta.get("cpu_temp_at")
        try:
            temp = float(temp_raw)
        except (TypeError, ValueError):
            continue
        if temp <= 0.0:
            continue
        if ts >= recent_floor:
            recent[cls_str].append(temp)
        else:
            trailing[cls_str].append(temp)

    out: dict[str, float] = {}
    for cls in recent.keys() & trailing.keys():
        if len(recent[cls]) < MIN_SAMPLES_PER_BUCKET:
            continue
        if len(trailing[cls]) < MIN_SAMPLES_PER_BUCKET:
            continue
        mean_recent = sum(recent[cls]) / len(recent[cls])
        mean_trailing = sum(trailing[cls]) / len(trailing[cls])
        out[cls] = mean_recent - mean_trailing
    return out


@dataclass
class DriftGate:
    """Stateful gate: tracks consecutive drift detections, fires refit signal.

    Parameters
    ----------
    min_consecutive
        How many consecutive drift detections are required before
        `should_refit()` returns True. Default: 3.
    min_gap_sec
        Minimum wall-clock gap between two analyses that count as
        «consecutive». Prevents rapid successive calls from gaming the
        counter. Default: 3600 (1 hour).

    Usage
    -----
    Call `record(drift_map, now=...)` after each `detect_cluster_drift`.
    `drift_map` is considered «drifting» when it is non-empty (i.e. at least
    one workload class shows measurable drift). Then call `should_refit()` to
    check whether the gate has accumulated enough consecutive drifts.
    Call `reset()` after a successful refit to clear the streak.
    """

    min_consecutive: int = DEFAULT_MIN_CONSECUTIVE
    min_gap_sec: float = DEFAULT_MIN_GAP_SEC
    _streak: list[float] = field(default_factory=list, repr=False)

    def record(self, drift_map: dict[str, float], *, now: float | None = None) -> None:
        """Record one analysis result.  Appends to streak on positive drift,
        resets on empty / non-drifting map.

        Note: `detect_cluster_drift` returns per-class temperature deltas in
        either direction.  We only count POSITIVE drift (cluster running
        hotter than the trailing baseline) toward a refit streak.  All-negative
        maps mean the system is cooling vs. its history — that's improvement,
        not drift; refitting the Embedder on it would burn CPU for nothing
        and risk a parity reject loop.
        """
        now_ts = now if now is not None else time.time()
        if not drift_map or not any(v > 0 for v in drift_map.values()):
            self._streak = []
            return
        if self._streak:
            gap = now_ts - self._streak[-1]
            if gap < self.min_gap_sec:
                # Too soon — update the timestamp in-place but don't extend streak.
                self._streak[-1] = now_ts
                return
        self._streak.append(now_ts)

    def should_refit(self) -> bool:
        """Return True when consecutive drift streak has reached the threshold."""
        return len(self._streak) >= self.min_consecutive

    def reset(self) -> None:
        """Clear streak — call after a successful refit."""
        self._streak = []

    @property
    def streak_len(self) -> int:
        return len(self._streak)
