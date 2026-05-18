"""Dump telemetry frames from store.db to csv/jsonl.

Schema (csv columns / jsonl keys):
  ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, workload_label

Time range via `since` (string like "1d", "15m", "2h").
"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import IO

_COLS: tuple[str, ...] = (
    "ts",
    "cpu_temp",
    "cpu_power",
    "gpu_temp",
    "gpu_power",
    "fan_max_rpm",
    "workload_label",
)


def _parse_window(since: str) -> float:
    """Convert string '15m'/'2h'/'1d' to seconds.

    Bare numbers are treated as seconds. Suffixes 's', 'm', 'h', 'd' are
    case-insensitive.
    """
    s = since.strip().lower()
    suffix = s[-1]
    if suffix.isalpha():
        n = float(s[:-1])
        if suffix == "s":
            return n
        if suffix == "m":
            return n * 60
        if suffix == "h":
            return n * 3600
        if suffix == "d":
            return n * 86400
        raise ValueError(f"unknown time suffix: {suffix!r}")
    return float(s)


def export_telemetry(store_path: Path, since: str, fmt: str, out: IO[str]) -> int:
    """Export frames from store.db over a time range. Returns row count written."""
    cutoff = time.time() - _parse_window(since)
    conn = sqlite3.connect(store_path)
    try:
        rows = conn.execute(
            "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
            "workload_label FROM frames WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    if fmt == "csv":
        w = csv.writer(out)
        w.writerow(_COLS)
        w.writerows(rows)
    elif fmt == "jsonl":
        for row in rows:
            out.write(json.dumps(dict(zip(_COLS, row, strict=True))) + "\n")
    else:
        raise ValueError(f"unknown fmt: {fmt!r}")
    return len(rows)
