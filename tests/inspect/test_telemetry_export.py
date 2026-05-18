"""Tests for coolstep/inspect/telemetry_export.py."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

from coolstep.inspect.telemetry_export import _parse_window, export_telemetry


def _make_store(path: Path, frames: list[tuple]) -> None:
    """Create a minimal store.db with `frames` table matching the export query."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE frames ("
            "ts REAL, cpu_temp REAL, cpu_power REAL, gpu_temp REAL, "
            "gpu_power REAL, fan_max_rpm INTEGER, workload_label TEXT)"
        )
        conn.executemany(
            "INSERT INTO frames "
            "(ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, workload_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            frames,
        )
        conn.commit()
    finally:
        conn.close()


def test_export_csv_writes_header_and_rows(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    now = time.time()
    _make_store(
        store,
        [
            (now - 60, 70.0, 25.0, 65.0, 80.0, 3000, "idle"),
            (now - 30, 72.0, 30.0, 66.0, 90.0, 3200, "compile"),
            (now, 75.0, 35.0, 68.0, 100.0, 3500, "game"),
        ],
    )
    buf = io.StringIO()
    n = export_telemetry(store, "1h", "csv", buf)
    assert n == 3
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 4  # header + 3
    assert lines[0].startswith("ts,cpu_temp,cpu_power,gpu_temp,gpu_power,fan_max_rpm,workload_label")
    assert "idle" in lines[1]
    assert "game" in lines[3]


def test_export_jsonl_one_per_line(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    now = time.time()
    _make_store(
        store,
        [
            (now - 60, 70.0, 25.0, 65.0, 80.0, 3000, "idle"),
            (now - 30, 72.0, 30.0, 66.0, 90.0, 3200, "compile"),
            (now, 75.0, 35.0, 68.0, 100.0, 3500, "game"),
        ],
    )
    buf = io.StringIO()
    n = export_telemetry(store, "1h", "jsonl", buf)
    assert n == 3
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        rec = json.loads(line)
        assert set(rec.keys()) == {
            "ts", "cpu_temp", "cpu_power", "gpu_temp", "gpu_power",
            "fan_max_rpm", "workload_label",
        }
    first = json.loads(lines[0])
    assert first["workload_label"] == "idle"


def test_export_since_filters_old_rows(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    now = time.time()
    _make_store(
        store,
        [
            (100.0, 70.0, 25.0, 65.0, 80.0, 3000, "old"),
            (now - 30, 72.0, 30.0, 66.0, 90.0, 3200, "recent"),
        ],
    )
    buf = io.StringIO()
    n = export_telemetry(store, "1h", "csv", buf)
    assert n == 1
    body = buf.getvalue()
    assert "recent" in body
    assert "old" not in body


def test_parse_window_units() -> None:
    assert _parse_window("15m") == 900.0
    assert _parse_window("2h") == 7200.0
    assert _parse_window("1d") == 86400.0
    assert _parse_window("30s") == 30.0
    assert _parse_window("60") == 60.0  # bare number = seconds


def test_export_unknown_fmt_raises(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    _make_store(store, [(time.time(), 70.0, 25.0, 65.0, 80.0, 3000, "x")])
    buf = io.StringIO()
    with pytest.raises(ValueError):
        export_telemetry(store, "1h", "xml", buf)
