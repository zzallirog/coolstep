"""Sqlite persistence for telemetry frames + throttle events + actions.

Schema follows docs/architecture.md. Rotation: frames > 14 days dropped on
each `rotate()`. throttle_events / actions kept 90 days.

Maintenance — manual offline VACUUM
-----------------------------------
`rotate()` DELETEs old frames but does not reclaim file pages, so the
store grows without bound until VACUUM runs. VACUUM is too expensive to
schedule in the tick loop (rewrites the whole DB; 3.6GB → tens of
seconds; needs free space ≥ current DB size). One-shot offline recipe:

    systemctl --user stop coolstep-collector
    sqlite3 ~/coolstep/data/store.db 'PRAGMA wal_checkpoint(TRUNCATE); VACUUM;'
    systemctl --user start coolstep-collector

Expected size: 3.6GB → ~1.5GB if raw_json is dominant filler; less if
column-level fragmentation. The `Store.vacuum()` method exposes the same
behaviour as an explicit API for maintenance scripts; do NOT wire it into
the tick loop or `rotate()`.
"""

from __future__ import annotations

import json
import os
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

-- Long-horizon thermal ledger. One row per (UTC day × workload label) +
-- an '_all' pooled row per day. NO TTL — this is the permanent record
-- that survives the 14-day frames eviction, so seasonal phase shifts
-- (winter/summer ambient), thermal-interface degradation and dust
-- build-up stay measurable years later: same workload bucket, rising
-- temp_p95 / fan_p95 at flat power_p50 = the cooling path got worse.
CREATE TABLE IF NOT EXISTS daily_rollup (
    day             TEXT NOT NULL,      -- YYYY-MM-DD (UTC)
    workload_label  TEXT NOT NULL,      -- '' = unlabeled, '_all' = pooled
    frame_count     INTEGER NOT NULL,
    cpu_temp_p50    REAL,
    cpu_temp_p95    REAL,
    cpu_temp_max    REAL,
    cpu_power_p50   REAL,
    cpu_power_p95   REAL,
    gpu_temp_p95    REAL,
    fan_rpm_p50     REAL,
    fan_rpm_p95     REAL,
    fan_rpm_max     REAL,
    throttle_events INTEGER NOT NULL DEFAULT 0,  -- only on '_all' rows
    PRIMARY KEY (day, workload_label)
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
        # Default wal_autocheckpoint=1000 pages (~4MB) only fires when an
        # unblocked writer triggers it; a long-lived dashboard reader can
        # keep the WAL pinned indefinitely. Bumping to 2000 pages (~8MB)
        # gives a tighter ceiling without thrashing the writer.
        self._conn.execute("PRAGMA wal_autocheckpoint=2000")
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
        # Roll up complete days BEFORE eviction — a day must land in the
        # permanent ledger before its frames can age out of the 14-day TTL.
        try:
            self.rollup_days(now=now_ts)
        except Exception:  # noqa: BLE001 — rollup must never block eviction
            pass
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
                # PASSIVE checkpoint flushes whatever pages are not pinned
                # by a reader; never blocks. TRUNCATE would block readers,
                # which we explicitly do not want on a 10-min routine.
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            self._writes_paused = False
        return frames_del, events_del

    def rollup_days(self, now: float | None = None) -> int:
        """Aggregate every COMPLETE UTC day not yet in `daily_rollup`.

        High-water mark lives in `meta['rollup_day_done']` (YYYY-MM-DD).
        Days are aggregated in Python (sqlite has no percentile): one
        day at 1 Hz ≈ 86 400 rows — fine on the rotate() worker thread.
        Returns the number of day-rows written. Idempotent: re-running a
        day REPLACEs the same primary keys.
        """
        import datetime as _dt

        now_ts = now if now is not None else time.time()
        today = _dt.datetime.fromtimestamp(now_ts, tz=_dt.timezone.utc).date()
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='rollup_day_done'"
            ).fetchone()
            first_ts_row = self._conn.execute(
                "SELECT MIN(ts) FROM frames"
            ).fetchone()
        if first_ts_row is None or first_ts_row[0] is None:
            return 0
        first_day = _dt.datetime.fromtimestamp(
            float(first_ts_row[0]), tz=_dt.timezone.utc
        ).date()
        if row is not None:
            done = _dt.date.fromisoformat(row[0])
            start_day = max(first_day, done + _dt.timedelta(days=1))
        else:
            start_day = first_day
        written = 0
        day = start_day
        while day < today:  # strictly complete days only
            written += self._rollup_one_day(day)
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) "
                    "VALUES ('rollup_day_done', ?)",
                    (day.isoformat(),),
                )
                self._conn.commit()
            day += _dt.timedelta(days=1)
        return written

    def _rollup_one_day(self, day) -> int:  # type: ignore[no-untyped-def]
        import datetime as _dt

        day_start = _dt.datetime.combine(
            day, _dt.time.min, tzinfo=_dt.timezone.utc
        ).timestamp()
        day_end = day_start + 86400.0
        with self._lock:
            rows = self._conn.execute(
                "SELECT workload_label, cpu_temp, cpu_power, gpu_temp, "
                "fan_max_rpm FROM frames WHERE ts >= ? AND ts < ?",
                (day_start, day_end),
            ).fetchall()
            throttle_count = int(self._conn.execute(
                "SELECT COUNT(*) FROM throttle_events "
                "WHERE ts_start >= ? AND ts_start < ?",
                (day_start, day_end),
            ).fetchone()[0])
        if not rows:
            return 0

        def _pct(sorted_vals: list[float], pct: float) -> float | None:
            if not sorted_vals:
                return None
            idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * pct / 100.0))
            return sorted_vals[idx]

        groups: dict[str, list[tuple]] = {"_all": []}
        for r in rows:
            label = r[0] or ""
            groups.setdefault(label, []).append(r)
            groups["_all"].append(r)

        out_rows = []
        for label, grp in groups.items():
            temps = sorted(r[1] for r in grp if r[1] is not None and r[1] > 0)
            powers = sorted(r[2] for r in grp if r[2] is not None and r[2] > 0)
            gpu_temps = sorted(r[3] for r in grp if r[3] is not None and r[3] > 0)
            fans = sorted(r[4] for r in grp if r[4] is not None and r[4] > 0)
            out_rows.append((
                day.isoformat(), label, len(grp),
                _pct(temps, 50), _pct(temps, 95),
                temps[-1] if temps else None,
                _pct(powers, 50), _pct(powers, 95),
                _pct(gpu_temps, 95),
                _pct(fans, 50), _pct(fans, 95),
                fans[-1] if fans else None,
                throttle_count if label == "_all" else 0,
            ))
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO daily_rollup "
                "(day, workload_label, frame_count, cpu_temp_p50, cpu_temp_p95, "
                " cpu_temp_max, cpu_power_p50, cpu_power_p95, gpu_temp_p95, "
                " fan_rpm_p50, fan_rpm_p95, fan_rpm_max, throttle_events) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                out_rows,
            )
            self._conn.commit()
        return len(out_rows)

    def read_rollup(
        self,
        since_day: str | None = None,
        workload_label: str | None = None,
        limit: int = 2000,
    ) -> list[dict[str, object]]:
        """Read the long-horizon ledger, newest day first."""
        sql = (
            "SELECT day, workload_label, frame_count, cpu_temp_p50, "
            "cpu_temp_p95, cpu_temp_max, cpu_power_p50, cpu_power_p95, "
            "gpu_temp_p95, fan_rpm_p50, fan_rpm_p95, fan_rpm_max, "
            "throttle_events FROM daily_rollup"
        )
        clauses, params = [], []
        if since_day is not None:
            clauses.append("day >= ?")
            params.append(since_day)
        if workload_label is not None:
            clauses.append("workload_label = ?")
            params.append(workload_label)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY day DESC, workload_label LIMIT ?"
        params.append(int(limit))
        cols = (
            "day", "workload_label", "frame_count", "cpu_temp_p50",
            "cpu_temp_p95", "cpu_temp_max", "cpu_power_p50", "cpu_power_p95",
            "gpu_temp_p95", "fan_rpm_p50", "fan_rpm_p95", "fan_rpm_max",
            "throttle_events",
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def vacuum(self) -> int:
        """Rewrite the DB to reclaim freed pages. Returns bytes freed.

        VACUUM is expensive: rewrites the entire database to a temp file
        then renames over the original (3.6GB → tens of seconds; needs
        free space ≥ current DB size). Call manually from a maintenance
        script — do NOT wire into `rotate()` or the tick loop.

        Pauses writes via `_writes_paused` for the duration so the
        daemon's per-tick callers skip rather than queue on the lock.
        Returns the byte delta (size_before - size_after); may be 0 or
        slightly negative if the file is already tightly packed.
        """
        self._writes_paused = True
        try:
            with self._lock:
                size_before = (
                    os.path.getsize(self.path) if self.path.exists() else 0
                )
                self._conn.execute("VACUUM")
                self._conn.commit()
                size_after = (
                    os.path.getsize(self.path) if self.path.exists() else 0
                )
        finally:
            self._writes_paused = False
        return size_before - size_after

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
