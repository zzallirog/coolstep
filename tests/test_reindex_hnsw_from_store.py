"""Smoke test for `scripts/reindex_hnsw_from_store.py`.

The reindex script is operationally critical (used during the chroma →
hnsw cutover and any future from-scratch rebuild) and had no test until
the v0.5.9 audit flagged it. A schema-drift bug in its
`_frame_from_jsonable` (independent copy from `embedder_refit.py`) would
silently produce empty embeddings — the cutover would "succeed" with
0 vectors and prediction quality would crater.

Strategy: spin up a tiny `store.db` with 100 fake frames in `tmp_path`,
invoke the script as a module, assert HnswStore on the result has > 0
vectors.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def _seed_store_db(path: Path, n: int = 120) -> None:
    """Create a minimal store.db with `n` frames that the reindex script
    can parse. Schema mirrors `coolstep/core/store.py:SCHEMA_SQL` only
    for the frames table — other tables are unused by reindex.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE frames ("
        " ts REAL PRIMARY KEY,"
        " cpu_temp REAL,"
        " raw_json TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE throttle_events ("
        " ts_start REAL PRIMARY KEY,"
        " ts_end REAL,"
        " peak_temp REAL"
        ")"
    )
    for i in range(n):
        ts = 1_700_000_000.0 + i
        cpu_temp = 60.0 + (i % 20) * 0.5  # 60–70°C
        payload = {
            "timestamp": ts,
            "cpu": {
                "temps_c": {"tctl": cpu_temp, "tdie": cpu_temp},
                "load_pct": [10.0 + (i % 5)] * 8,
            },
            "gpus": [],
            "fans": [{"name": "cpu", "rpm": 1500 + (i % 100) * 10}],
            "storage_temps_c": {},
            "memory_temps_c": {},
            "workload": {
                "top_processes": [],
                "rolling_features": {},
                "label": "idle",
            },
            "platform_state": {},
        }
        conn.execute(
            "INSERT INTO frames (ts, cpu_temp, raw_json) VALUES (?, ?, ?)",
            (ts, cpu_temp, json.dumps(payload)),
        )
    conn.commit()
    conn.close()


def test_reindex_hnsw_from_store_builds_index(tmp_path: Path) -> None:
    pytest.importorskip("hnswlib")

    store_db = tmp_path / "store.db"
    _seed_store_db(store_db, n=120)

    script = Path(__file__).resolve().parents[1] / "scripts" / "reindex_hnsw_from_store.py"
    assert script.exists(), f"reindex script not found at {script}"

    env_home = str(tmp_path)
    # The script reads COOLSTEP_HOME for both the source store.db and the
    # destination HnswStore dir — point both at our isolated tmp_path so
    # we don't touch the real ~/coolstep/data.
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--store",
            str(store_db),
            "--fit-frames",
            "30",
            "--batch-log",
            "1000",
        ],
        env={"COOLSTEP_HOME": env_home, "PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"reindex exited {result.returncode}\nstdout:\n{result.stdout}"
        f"\nstderr:\n{result.stderr}"
    )

    # The script writes to <COOLSTEP_HOME>/hnsw/ — verify it exists and is
    # populated.
    from coolstep.adapters.storage.hnsw import HnswStore

    hnsw_dir = Path(env_home) / "hnsw"
    assert hnsw_dir.exists(), "reindex did not create hnsw/ dir"

    store = HnswStore(persist_dir=hnsw_dir)
    assert store.discover()
    try:
        # All 120 frames had a valid cpu_temp, so we expect 120 in the index.
        assert store.count() == 120, (
            f"expected 120 vectors, got {store.count()}; "
            f"reindex schema drifted from frame payload"
        )
    finally:
        store.close()
