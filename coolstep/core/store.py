"""Sqlite persistence for telemetry frames + throttle events + actions.

Schema follows docs/architecture.md. Rotation: frames > 14 days dropped on
each `rotate()`. throttle_events / actions kept 90 days.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from threading import RLock

from coolstep.core._helpers import canonical_cpu_temp
from coolstep.core.schema import TelemetryFrame

FRAMES_TTL_SEC = 14 * 24 * 3600
EVENTS_TTL_SEC = 90 * 24 * 3600

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS frames (
    ts          REAL PRIMARY KEY,
    cpu_temp    REAL,
    cpu_power   REAL,
    gpu_temp    REAL,
    gpu_power   REAL,
    fan_max_rpm INTEGER,
    workload_label TEXT,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts);

CREATE TABLE IF NOT EXISTS throttle_events (
    ts_start    REAL PRIMARY KEY,
    ts_end      REAL,
    duration    REAL,
    peak_temp   REAL,
    cause_label TEXT,
    workload_at_start TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    ts          REAL PRIMARY KEY,
    verb        TEXT,
    params      TEXT,
    actuator    TEXT,
    dry_run     INTEGER,
    result      TEXT,
    before_temp REAL,
    after_temp  REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """Thread-safe sqlite wrapper. Keep one Store per process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = RLock()
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        # Set True while `rotate()` is holding the writer lock for the bulk
        # DELETE pass. Per-tick callers (daemon `write_frame`) check this
        # flag and skip the write so the tick coroutine doesn't queue on
        # the lock; missing 1-2 frames per 10 min is acceptable, and the
        # tick after rotate finishes captures the next frame normally.
        self._writes_paused: bool = False

    @property
    def writes_paused(self) -> bool:
        return self._writes_paused

    def write_frame(self, frame: TelemetryFrame) -> None:
        # G-9 — vendor-agnostic CPU temp shortcut.  Prior code took only AMD
        # sensors (Tctl/Tdie); Intel coretemp emits "Package id 0" instead,
        # so an Intel host wrote cpu_temp=NULL to sqlite while temps_c['package']
        # was sitting right there with the real value.  Fallback chain:
        # AMD primary → AMD secondary → Intel package → max-of-cores universal.
        cpu_temp = canonical_cpu_temp(frame)
        cpu_power = frame.cpu.power_w.get("package")
        gpu_temp = max((g.temp_c for g in frame.gpus if g.temp_c is not None), default=None)
        gpu_power = max((g.power_w for g in frame.gpus if g.power_w is not None), default=None)
        fan_rpm = max((f.rpm for f in frame.fans if f.rpm is not None), default=None)
        label = frame.workload.label if frame.workload else None
        raw = json.dumps(_frame_to_jsonable(frame), separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO frames
                (ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm,
                 workload_label, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    frame.timestamp,
                    cpu_temp,
                    cpu_power,
                    gpu_temp,
                    gpu_power,
                    fan_rpm,
                    label,
                    raw,
                ),
            )
            self._conn.commit()

    def write_throttle_event(
        self,
        ts_start: float,
        ts_end: float,
        peak_temp: float,
        cause_label: str,
        workload_at_start: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO throttle_events
                (ts_start, ts_end, duration, peak_temp, cause_label, workload_at_start)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_start,
                    ts_end,
                    ts_end - ts_start,
                    peak_temp,
                    cause_label,
                    workload_at_start,
                ),
            )
            self._conn.commit()

    def count_frames(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM frames")
        return int(cur.fetchone()[0])

    def count_throttle_events(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM throttle_events")
        return int(cur.fetchone()[0])

    def count_throttle_events_since(self, ts_floor: float) -> int:
        """Throttle events whose `ts_start` is at or after `ts_floor`.

        Used by the daemon's adaptive curve to detect a recently-hot
        chip and add a small persistent fan-curve boost via the
        `recent_throttle_bump` policy."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM throttle_events WHERE ts_start >= ?",
            (float(ts_floor),),
        )
        return int(cur.fetchone()[0])

    def coverage_seconds(self) -> float:
        cur = self._conn.execute("SELECT MIN(ts), MAX(ts) FROM frames")
        row = cur.fetchone()
        if row[0] is None or row[1] is None:
            return 0.0
        return float(row[1]) - float(row[0])

    def latest_frame_row(self) -> dict[str, object] | None:
        cur = self._conn.execute(
            "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
            "workload_label FROM frames ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = (
            "ts",
            "cpu_temp",
            "cpu_power",
            "gpu_temp",
            "gpu_power",
            "fan_max_rpm",
            "workload_label",
        )
        return dict(zip(cols, row, strict=True))

    def rotate(self, now: float | None = None) -> tuple[int, int]:
        """Drop frames older than FRAMES_TTL_SEC, events older than EVENTS_TTL_SEC.

        Returns (frames_deleted, events_deleted).

        Sets `_writes_paused` for the duration of the bulk DELETE so the
        daemon's per-tick `write_frame` callers skip rather than queue on
        the writer lock — see `_writes_paused` on `__init__`.
        """
        now_ts = now if now is not None else time.time()
        self._writes_paused = True
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM frames WHERE ts < ?", (now_ts - FRAMES_TTL_SEC,)
                )
                frames_del = cur.rowcount or 0
                cur = self._conn.execute(
                    "DELETE FROM throttle_events WHERE ts_start < ?",
                    (now_ts - EVENTS_TTL_SEC,),
                )
                events_del = cur.rowcount or 0
                self._conn.commit()
        finally:
            self._writes_paused = False
        return frames_del, events_del

    def close(self) -> None:
        self._conn.close()


def _frame_to_jsonable(frame: TelemetryFrame) -> dict[str, object]:
    """Convert dataclass tree to plain dict suitable for json.dumps."""
    return {
        "timestamp": frame.timestamp,
        "cpu": asdict(frame.cpu),
        "gpus": [asdict(g) for g in frame.gpus],
        "fans": [asdict(f) for f in frame.fans],
        "storage_temps_c": dict(frame.storage_temps_c),
        "memory_temps_c": dict(frame.memory_temps_c),
        "workload": asdict(frame.workload) if frame.workload else None,
        "platform_state": dict(frame.platform_state),
    }
