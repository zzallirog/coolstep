"""Tests for coolstep/inspect/history.py."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coolstep.inspect.history import format_summary, summarise


def _write_records(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_summarise_empty_file_returns_zero_total(tmp_path: Path) -> None:
    p = tmp_path / "decisions.jsonl"
    p.write_text("")
    s = summarise(p, since_seconds=3600)
    assert s["total"] == 0
    assert s["fires"] == 0
    assert s["verbs"] == {}
    assert s["top_reasons"] == []
    assert s["first_ts"] is None
    assert s["last_ts"] is None


def test_summarise_missing_file_returns_error(tmp_path: Path) -> None:
    p = tmp_path / "does_not_exist.jsonl"
    s = summarise(p, since_seconds=3600)
    assert "error" in s
    assert "missing" in s["error"]


def test_summarise_counts_fires_and_verbs(tmp_path: Path) -> None:
    p = tmp_path / "decisions.jsonl"
    now = time.time()
    _write_records(
        p,
        [
            {
                "ts": now - 60,
                "prediction": {"throttle_prob": 0.8, "reason": "hot"},
                "actions": [{"verb": "ramp_cooling", "params": {}, "expires_at": 0}],
            },
            {
                "ts": now - 30,
                "prediction": {"throttle_prob": 0.95, "reason": "very hot"},
                "actions": [{"verb": "cap_boost", "params": {}, "expires_at": 0}],
            },
            {
                "ts": now - 5,
                "prediction": {"throttle_prob": 0.1, "reason": "cool"},
                "actions": [],
            },
        ],
    )
    s = summarise(p, since_seconds=3600)
    assert s["total"] == 3
    assert s["fires"] == 2
    assert s["verbs"] == {"ramp_cooling": 1, "cap_boost": 1}
    assert s["fire_rate"] == 2 / 3
    # top_reasons preserved as list of (str, int) tuples
    reasons = dict(s["top_reasons"])
    assert reasons == {"hot": 1, "very hot": 1, "cool": 1}
    # prob_dist populated
    assert s["prob_dist"]["max"] == 0.95


def test_summarise_filters_by_since(tmp_path: Path) -> None:
    p = tmp_path / "decisions.jsonl"
    now = time.time()
    _write_records(
        p,
        [
            {
                "ts": 100.0,  # ancient
                "prediction": {"throttle_prob": 0.5, "reason": "old"},
                "actions": [],
            },
            {
                "ts": now - 60,
                "prediction": {"throttle_prob": 0.9, "reason": "recent"},
                "actions": [{"verb": "ramp_cooling", "params": {}, "expires_at": 0}],
            },
        ],
    )
    s = summarise(p, since_seconds=3600)
    assert s["total"] == 1
    assert s["fires"] == 1
    assert dict(s["top_reasons"]) == {"recent": 1}


def test_format_summary_renders_table(tmp_path: Path) -> None:
    p = tmp_path / "decisions.jsonl"
    now = time.time()
    _write_records(
        p,
        [
            {
                "ts": now - 10,
                "prediction": {"throttle_prob": 0.75, "reason": "warm"},
                "actions": [{"verb": "ramp_cooling", "params": {}, "expires_at": 0}],
            },
        ],
    )
    s = summarise(p, since_seconds=3600)
    text = format_summary(s)
    assert "decisions:" in text
    assert "ramp_cooling" in text
    assert "throttle_prob" in text


def test_format_summary_empty_and_error() -> None:
    assert "no decisions" in format_summary(
        {
            "total": 0, "fires": 0, "verbs": {}, "top_reasons": [],
            "prob_dist": {}, "first_ts": None, "last_ts": None,
        }
    )
    assert "missing" in format_summary({"error": "decisions.jsonl missing"})


def test_summarise_skips_invalid_json_lines(tmp_path: Path) -> None:
    p = tmp_path / "decisions.jsonl"
    now = time.time()
    text = json.dumps(
        {
            "ts": now - 10,
            "prediction": {"throttle_prob": 0.8, "reason": "ok"},
            "actions": [],
        }
    )
    p.write_text(text + "\n{not valid json\n" + text + "\n")
    s = summarise(p, since_seconds=3600)
    assert s["total"] == 2
