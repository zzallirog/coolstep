#!/usr/bin/env python3
"""bench/analyze.py — coolstep stress-run analyser (P2.3).

Reads a bench/runs/<ts>-<scenario>/ directory produced by bench/stress.sh.
Prints a deterministic, human-readable + parseable summary to stdout.

Metric of record
----------------
  delta = t_first_fan_rampup - t_first_predict
  Where:
    t_first_fan_rampup = first sample where fan_max_rpm >= FAN_RAMPUP_RPM_THRESHOLD
    t_first_predict    = first sample where throttle_prob >= THROTTLE_PROB_THRESHOLD
  delta >= COOLSTEP_MOVED_FIRST_MIN_SEC  → "coolstep moved first"
  delta positive but < threshold         → "lead exists but below 8 s target"
  delta <= 0                             → "coolstep did NOT move first"

Field paths
-----------
  telemetry.jsonl  : fan_max_rpm (int, top-level)
                     cpu_temp    (float, top-level)
  ml-state.jsonl   : throttle_prob (float, top-level — NOT nested)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Thresholds (single config surface — update these if PLAN §2 changes)
# ---------------------------------------------------------------------------
THROTTLE_PROB_THRESHOLD: float = 0.65       # PLAN §2 trigger contract
FAN_RAMPUP_RPM_THRESHOLD: int = 4500        # «rampup» = fans spinning up under load
COOLSTEP_MOVED_FIRST_MIN_SEC: float = 8.0   # «metric of record» target
HOT_TEMP_THRESHOLD_C: float = 85.0          # time-above-85°C metric
SAMPLE_INTERVAL_S: float = 2.0              # nominal sampler cadence


# ---------------------------------------------------------------------------
# Sparkline helpers
# ---------------------------------------------------------------------------
_SPARKS = "▁▂▃▄▅▆▇█"
_SPARK_WIDTH = 40


def _sparkline(values: list[float], width: int = _SPARK_WIDTH) -> str:
    """Return a fixed-width ASCII sparkline string from a list of floats."""
    if not values:
        return "─" * width
    mn = min(values)
    mx = max(values)
    span = mx - mn or 1.0

    # Downsample or upsample to `width` buckets
    buckets: list[list[float]] = [[] for _ in range(width)]
    n = len(values)
    for i, v in enumerate(values):
        bucket_idx = min(int(i * width / n), width - 1)
        buckets[bucket_idx].append(v)

    chars = []
    for bucket in buckets:
        avg = statistics.mean(bucket) if bucket else mn
        idx = int((avg - mn) / span * (len(_SPARKS) - 1))
        chars.append(_SPARKS[idx])
    return "".join(chars)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _load_jsonl(path: Path) -> list[dict]:
    """Load a .jsonl file, silently skip malformed lines."""
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _load_meta(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _safe_float(row: dict, *keys: str) -> float | None:
    """Traverse nested keys, return float or None. Never raises."""
    obj = row
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    if obj is None:
        return None
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _safe_int(row: dict, *keys: str) -> int | None:
    v = _safe_float(row, *keys)
    return int(v) if v is not None else None


def analyse(run_dir: Path) -> int:
    """Analyse a run directory. Returns 0 on success, 1 on missing/empty data."""
    tel_rows = _load_jsonl(run_dir / "telemetry.jsonl")
    ml_rows = _load_jsonl(run_dir / "ml-state.jsonl")
    meta = _load_meta(run_dir / "meta.json")

    if not tel_rows and not ml_rows:
        print(f"ERROR: no data in {run_dir}", file=sys.stderr)
        return 1

    # -- Run metadata --
    scenario = meta.get("scenario", run_dir.name.split("-", 1)[-1] if "-" in run_dir.name else "?")
    armed = meta.get("armed", "?")
    duration_sec = meta.get("duration_sec", "?")
    started_at = meta.get("started_at")
    hypr_start = meta.get("hypr_sig_start", "?")
    hypr_end = meta.get("hypr_sig_end", "?")

    run_start_ts: float | None = None
    if started_at is not None:
        try:
            run_start_ts = float(started_at)
        except (TypeError, ValueError):
            pass

    # Fall back to first recorded_at in telemetry
    if run_start_ts is None and tel_rows:
        run_start_ts = _safe_float(tel_rows[0], "recorded_at") or _safe_float(tel_rows[0], "ts")

    print("=" * 60)
    print(f"  Run: {run_dir.name}")
    print(f"  Scenario : {scenario}")
    print(f"  Armed    : {armed}")
    print(f"  Duration : {duration_sec}s planned")
    print(f"  Hypr sig : {hypr_start} → {hypr_end}")
    print(f"  Telemetry rows : {len(tel_rows)}")
    print(f"  ML-state  rows : {len(ml_rows)}")
    print("=" * 60)

    # -- cpu_temp series --
    cpu_temps: list[float] = []
    for row in tel_rows:
        v = _safe_float(row, "cpu_temp")
        if v is not None:
            cpu_temps.append(v)

    peak_tctl = max(cpu_temps) if cpu_temps else None
    if peak_tctl is not None:
        print(f"\n  Peak Tctl        : {peak_tctl:.1f}°C")
    else:
        print("\n  Peak Tctl        : n/a")

    # -- time above 85°C --
    # Each sample covers ~SAMPLE_INTERVAL_S seconds (nominal)
    samples_above_85 = sum(1 for t in cpu_temps if t >= HOT_TEMP_THRESHOLD_C)
    time_above_85 = samples_above_85 * SAMPLE_INTERVAL_S
    print(f"  Time ≥ {HOT_TEMP_THRESHOLD_C:.0f}°C      : {time_above_85:.0f}s  ({samples_above_85} samples × {SAMPLE_INTERVAL_S:.0f}s)")

    # -- first throttle_prob >= 0.65 --
    first_predict_ts: float | None = None
    for row in ml_rows:
        prob = _safe_float(row, "throttle_prob")
        if prob is None:
            continue
        if prob >= THROTTLE_PROB_THRESHOLD:
            ts = _safe_float(row, "recorded_at") or _safe_float(row, "ts")
            if ts is not None:
                first_predict_ts = ts
                break

    if first_predict_ts is not None and run_start_ts is not None:
        rel = first_predict_ts - run_start_ts
        print(f"  First predict ≥{THROTTLE_PROB_THRESHOLD} : +{rel:.1f}s from run start  (ts={first_predict_ts:.3f})")
    else:
        print(f"  First predict ≥{THROTTLE_PROB_THRESHOLD} : not observed")

    # -- first fan rampup >= 4500 RPM --
    # telemetry/latest returns fan_max_rpm as a top-level integer field
    first_rampup_ts: float | None = None
    for row in tel_rows:
        rpm = _safe_int(row, "fan_max_rpm")
        if rpm is None:
            # Also try raw.fans array (from raw_json expansion)
            raw = row.get("raw") or {}
            fans = raw.get("fans") or []
            if fans:
                rpms = [f.get("rpm") for f in fans if isinstance(f, dict) and f.get("rpm") is not None]
                rpm = max(rpms) if rpms else None
        if rpm is not None and rpm >= FAN_RAMPUP_RPM_THRESHOLD:
            ts = _safe_float(row, "recorded_at") or _safe_float(row, "ts")
            if ts is not None:
                first_rampup_ts = ts
                break

    if first_rampup_ts is not None and run_start_ts is not None:
        rel = first_rampup_ts - run_start_ts
        print(f"  First fan rampup : +{rel:.1f}s from run start  (ts={first_rampup_ts:.3f})")
    else:
        print(f"  First fan rampup : not observed (threshold={FAN_RAMPUP_RPM_THRESHOLD} RPM)")

    # -- delta: metric of record --
    print()
    if first_predict_ts is not None and first_rampup_ts is not None:
        delta = first_rampup_ts - first_predict_ts
        if delta >= COOLSTEP_MOVED_FIRST_MIN_SEC:
            verdict = f"coolstep moved first by {delta:.1f}s  ✓ (target ≥{COOLSTEP_MOVED_FIRST_MIN_SEC:.0f}s)"
        elif delta > 0:
            verdict = f"coolstep lead: {delta:.1f}s  ✗ (below {COOLSTEP_MOVED_FIRST_MIN_SEC:.0f}s target)"
        else:
            verdict = f"coolstep did NOT move first  (delta={delta:.1f}s, fans already up)"
        print(f"  METRIC delta     : {verdict}")
    elif first_predict_ts is None and first_rampup_ts is None:
        print("  METRIC delta     : n/a — neither predict nor rampup observed")
    elif first_predict_ts is None:
        print("  METRIC delta     : n/a — predict not observed (model cold?)")
    else:
        print(f"  METRIC delta     : fans never reached {FAN_RAMPUP_RPM_THRESHOLD} RPM threshold")

    # -- sparkline --
    print()
    if cpu_temps:
        spark = _sparkline(cpu_temps)
        mn = min(cpu_temps)
        mx = max(cpu_temps)
        print(f"  cpu_temp spark ({mn:.0f}–{mx:.0f}°C, {len(cpu_temps)} pts):")
        print(f"  {spark}")
    else:
        print("  cpu_temp spark   : no data")

    print()
    print("  Thresholds (analyze.py constants):")
    print(f"    THROTTLE_PROB_THRESHOLD      = {THROTTLE_PROB_THRESHOLD}")
    print(f"    FAN_RAMPUP_RPM_THRESHOLD     = {FAN_RAMPUP_RPM_THRESHOLD} RPM")
    print(f"    COOLSTEP_MOVED_FIRST_MIN_SEC = {COOLSTEP_MOVED_FIRST_MIN_SEC}s")
    print(f"    HOT_TEMP_THRESHOLD_C         = {HOT_TEMP_THRESHOLD_C}°C")
    print("=" * 60)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench/analyze.py",
        description=(
            "Analyse a bench/runs/<ts>-<scenario>/ directory from bench/stress.sh.\n"
            "Prints: peak Tctl, time-above-85°C, first predict ts, first fan rampup ts,\n"
            "delta (metric of record), ASCII sparkline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "run_dir",
        help="Path to the run directory (e.g. bench/runs/20260511T143000-S1/)",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"ERROR: not a directory: {run_dir}", file=sys.stderr)
        sys.exit(1)
    sys.exit(analyse(run_dir))


if __name__ == "__main__":
    main()
