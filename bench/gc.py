#!/usr/bin/env python3
"""bench/gc.py — prune old stress-test runs + rebuild bench/runs/index.json.

Usage:
    bench/gc.py [--keep N] [--dry-run] [--runs-dir PATH]

By default: keeps newest 20 runs (by mtime), deletes the rest. Always rebuilds
bench/runs/index.json with one record per surviving run.

index.json shape:
{
  "rebuilt_at": <unix ts>,
  "count": <N>,
  "runs": [
    {
      "dir": "20260512T010101-S1",
      "scenario": "S1",
      "armed": true,
      "duration_sec": 120,
      "started_at": <ts>,
      "peak_tctl": 93.5,
      "time_above_85": 18,
      "first_predict_age": null,   // sec from run start
      "first_rampup_age": null,
      "delta_sec": null,           // first_rampup - first_predict, or null
      "verdict": "coolstep_moved_first" | "not_observed" | "did_not_move_first",
    },
    ...
  ]
}

Runs sorted by `started_at` descending.
"""

import argparse
import contextlib
import json
import shutil
import sys
import time
from pathlib import Path

# Thresholds — mirror bench/analyze.py constants.
_THROTTLE_PROB_THRESHOLD = 0.65
_FAN_RAMPUP_RPM_THRESHOLD = 4500
_COOLSTEP_MOVED_FIRST_MIN_SEC = 8.0
_HOT_TEMP_THRESHOLD_C = 85.0
_SAMPLER_PERIOD_SEC = 2


def _read_jsonl(path: Path) -> list[dict]:
    """Read a .jsonl file, skipping malformed lines. Returns [] on OSError."""
    rows: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                rows.append(json.loads(line))
    except OSError:
        pass
    return rows


def _parse_telemetry(tel_p: Path, started_at: float) -> tuple[float | None, int, float | None]:
    """Return (peak_tctl, time_above_85_sec, first_rampup_age_sec)."""
    peak = 0.0
    above_85_sec = 0
    first_rampup: float | None = None
    for row in _read_jsonl(tel_p):
        cpu_temp = float(row.get("cpu_temp", 0.0) or 0.0)
        if cpu_temp > peak:
            peak = cpu_temp
        if cpu_temp >= _HOT_TEMP_THRESHOLD_C:
            above_85_sec += _SAMPLER_PERIOD_SEC
        rpm = row.get("fan_max_rpm") or 0
        if isinstance(rpm, (int, float)) and rpm >= _FAN_RAMPUP_RPM_THRESHOLD and first_rampup is None:
            ts = float(row.get("ts", row.get("recorded_at", 0.0)) or 0.0)
            if started_at and ts:
                first_rampup = ts - started_at
    peak_tctl = peak if peak > 0 else None
    return peak_tctl, above_85_sec, first_rampup


def _parse_ml_state(ml_p: Path, started_at: float) -> float | None:
    """Return first_predict_age_sec (age from run start) or None."""
    for row in _read_jsonl(ml_p):
        prob = float(row.get("throttle_prob", 0.0) or 0.0)
        if prob >= _THROTTLE_PROB_THRESHOLD:
            ts = float(row.get("ts", row.get("recorded_at", 0.0)) or 0.0)
            if started_at and ts:
                return ts - started_at
    return None


def _compute_verdict(fp: float | None, fr: float | None) -> tuple[float | None, str]:
    """Return (delta_sec, verdict) from first_predict_age and first_rampup_age."""
    if fp is None and fr is None:
        return None, "not_observed"
    if fp is not None and fr is not None:
        delta = fr - fp
        verdict = "coolstep_moved_first" if delta >= _COOLSTEP_MOVED_FIRST_MIN_SEC else "did_not_move_first"
        return delta, verdict
    return None, "partial_data"


def _parse_run_metrics(run_dir: Path) -> dict:
    """Extract metrics from a single run directory.

    Reads:
      - meta.json — scenario, armed, duration_sec, started_at
      - telemetry.jsonl — for peak_tctl, time_above_85, first_rampup_age
      - ml-state.jsonl — for first_predict_age

    Best-effort: missing files / fields → null in output. Never raises.
    """
    record: dict = {
        "dir": run_dir.name,
        "scenario": None,
        "armed": None,
        "duration_sec": None,
        "started_at": None,
        "peak_tctl": None,
        "time_above_85": None,
        "first_predict_age": None,
        "first_rampup_age": None,
        "delta_sec": None,
        "verdict": "not_observed",
    }

    meta_p = run_dir / "meta.json"
    if meta_p.exists():
        try:
            m = json.loads(meta_p.read_text())
            record["scenario"] = m.get("scenario")
            record["armed"] = m.get("armed")
            record["duration_sec"] = m.get("duration_sec")
            record["started_at"] = m.get("started_at")
        except (OSError, json.JSONDecodeError):
            pass

    started_at = float(record["started_at"] or 0.0)

    tel_p = run_dir / "telemetry.jsonl"
    if tel_p.exists():
        peak_tctl, time_above_85, first_rampup = _parse_telemetry(tel_p, started_at)
        record["peak_tctl"] = peak_tctl
        record["time_above_85"] = time_above_85
        record["first_rampup_age"] = first_rampup

    ml_p = run_dir / "ml-state.jsonl"
    if ml_p.exists():
        record["first_predict_age"] = _parse_ml_state(ml_p, started_at)

    delta, verdict = _compute_verdict(record["first_predict_age"], record["first_rampup_age"])
    record["delta_sec"] = delta
    record["verdict"] = verdict
    return record


def _list_run_dirs(runs_dir: Path) -> list[Path]:
    """Return run directories sorted newest-first (by mtime)."""
    candidates = [
        child
        for child in runs_dir.iterdir()
        if child.is_dir() and child.name[:4].isdigit()
    ]
    candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return candidates


def main(argv=None):
    p = argparse.ArgumentParser(description="prune old stress-test runs + rebuild index.json")
    p.add_argument("--keep", type=int, default=20, help="newest N runs to keep")
    p.add_argument("--dry-run", action="store_true", help="don't delete, just report")
    p.add_argument("--runs-dir", default=None, help="override bench/runs path")
    args = p.parse_args(argv)

    runs_dir = Path(args.runs_dir) if args.runs_dir else Path(__file__).resolve().parent / "runs"

    if not runs_dir.is_dir():
        print(f"runs dir {runs_dir} not found", file=sys.stderr)
        return 1

    candidates = _list_run_dirs(runs_dir)
    keepers = candidates[: args.keep]
    losers = candidates[args.keep :]

    print(f"runs: {len(candidates)} total, keeping {len(keepers)}, pruning {len(losers)}")

    for d in losers:
        if args.dry_run:
            print(f"  would prune {d.name}")
        else:
            try:
                shutil.rmtree(d)
                print(f"  pruned {d.name}")
            except OSError as exc:
                print(f"  failed to prune {d.name}: {exc!r}", file=sys.stderr)

    # Rebuild index.json over keepers only.
    records = [_parse_run_metrics(d) for d in keepers]
    records.sort(key=lambda r: r.get("started_at") or 0.0, reverse=True)

    index_p = runs_dir / "index.json"
    if not args.dry_run:
        index_p.write_text(
            json.dumps(
                {
                    "rebuilt_at": time.time(),
                    "count": len(records),
                    "runs": records,
                },
                indent=2,
            )
        )
        print(f"wrote {index_p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
