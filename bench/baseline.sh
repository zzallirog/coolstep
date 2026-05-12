#!/usr/bin/env bash
# bench/baseline.sh — paired dry-run vs armed comparison for the same scenario.
#
# Runs stress.sh twice (once unarmed for baseline, once armed for treatment),
# analyzes both, prints side-by-side metric diff. The metric of record is
# delta_sec = (first_fan_rampup - first_predict_age) — positive means
# coolstep got the fans spinning before they would have otherwise.
#
# Usage:
#   bench/baseline.sh S1                  # 2 × 120s run
#   bench/baseline.sh S1 --duration 60    # 2 × 60s run (shorter for quick check)
#   bench/baseline.sh S2 --duration 90    # iowait scenario, custom length
#
# Both runs go into bench/runs/. The script prints both run-dir paths at end.

set -euo pipefail

SCENARIO="${1:?usage: $0 S1|S2|S3|S4 [--duration N]}"
shift || true

DURATION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --duration) shift; DURATION="$1" ;;
        *)          echo "unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STRESS="${REPO_ROOT}/bench/stress.sh"
ANALYZE="${REPO_ROOT}/bench/analyze.py"

if ! [ -x "$STRESS" ]; then
    echo "ERROR: $STRESS not executable" >&2
    exit 1
fi
if ! [ -x "$ANALYZE" ]; then
    echo "ERROR: $ANALYZE not executable" >&2
    exit 1
fi

echo "==> coolstep PAIRED comparison: $SCENARIO"
echo ""

# Build duration args
DUR_ARGS=()
if [ -n "$DURATION" ]; then
    DUR_ARGS=(--duration "$DURATION")
fi

# 1) DRY-RUN run (baseline)
echo "--- (1/2) baseline (dry-run) ---"
"$STRESS" "$SCENARIO" --dry-run "${DUR_ARGS[@]}" 2>&1 | tee /tmp/baseline-stress-1.log
RUN_DRY="$(grep -oE 'bench/runs/[0-9TS-]+' /tmp/baseline-stress-1.log | tail -1)"
if [ -z "$RUN_DRY" ]; then
    echo "ERROR: failed to extract dry-run dir from stress output" >&2
    exit 1
fi

# Brief recovery pause so the chip cools down between runs
echo ""
echo "...cooldown 30s between runs..."
sleep 30
echo ""

# 2) ARMED run (treatment)
echo "--- (2/2) treatment (armed) ---"
"$STRESS" "$SCENARIO" --armed "${DUR_ARGS[@]}" 2>&1 | tee /tmp/baseline-stress-2.log
RUN_ARMED="$(grep -oE 'bench/runs/[0-9TS-]+' /tmp/baseline-stress-2.log | tail -1)"
if [ -z "$RUN_ARMED" ]; then
    echo "ERROR: failed to extract armed dir from stress output" >&2
    exit 1
fi

# 3) Analyze both
echo ""
echo "===================================================================="
echo "  PAIRED COMPARISON  ($SCENARIO)"
echo "===================================================================="
echo ""

echo "--- BASELINE (dry-run) ---"
"$ANALYZE" "$REPO_ROOT/$RUN_DRY" 2>&1 || echo "(analyze failed for baseline)"
echo ""
echo "--- TREATMENT (armed) ---"
"$ANALYZE" "$REPO_ROOT/$RUN_ARMED" 2>&1 || echo "(analyze failed for treatment)"
echo ""

# 4) Side-by-side summary via python
python3 - <<PY
import json, sys
from pathlib import Path

baseline = Path("$REPO_ROOT/$RUN_DRY")
armed = Path("$REPO_ROOT/$RUN_ARMED")

def _extract(run_dir: Path) -> dict:
    """Return {peak, above_85, first_predict, first_rampup, delta} or empty dict."""
    out = {"peak": None, "above_85": None, "first_predict": None, "first_rampup": None, "delta": None}
    tel_p = run_dir / "telemetry.jsonl"
    ml_p = run_dir / "ml-state.jsonl"
    meta_p = run_dir / "meta.json"
    if not (tel_p.exists() and meta_p.exists()):
        return out
    try:
        meta = json.loads(meta_p.read_text())
        started_at = float(meta.get("started_at") or 0.0)
    except (OSError, json.JSONDecodeError):
        return out
    # Telemetry
    peak = 0.0
    above_85 = 0
    first_rampup = None
    try:
        for line in tel_p.read_text().splitlines():
            try: row = json.loads(line)
            except json.JSONDecodeError: continue
            cpu_temp = float(row.get("cpu_temp", 0) or 0)
            if cpu_temp > peak: peak = cpu_temp
            if cpu_temp >= 85: above_85 += 2
            rpm = row.get("fan_max_rpm") or 0
            if isinstance(rpm, (int, float)) and rpm >= 4500 and first_rampup is None:
                ts = float(row.get("ts", row.get("recorded_at", 0)) or 0)
                if started_at and ts: first_rampup = ts - started_at
    except OSError: pass
    out["peak"] = peak if peak > 0 else None
    out["above_85"] = above_85
    out["first_rampup"] = first_rampup
    # ml-state
    if ml_p.exists():
        first_predict = None
        try:
            for line in ml_p.read_text().splitlines():
                try: row = json.loads(line)
                except json.JSONDecodeError: continue
                prob = float(row.get("throttle_prob", 0) or 0)
                if prob >= 0.65 and first_predict is None:
                    ts = float(row.get("ts", row.get("recorded_at", 0)) or 0)
                    if started_at and ts: first_predict = ts - started_at
            out["first_predict"] = first_predict
        except OSError: pass
    if out["first_predict"] is not None and out["first_rampup"] is not None:
        out["delta"] = out["first_rampup"] - out["first_predict"]
    return out

b = _extract(baseline)
a = _extract(armed)

def _fmt(v, suf=""):
    if v is None: return "—"
    if isinstance(v, float): return f"{v:.1f}{suf}"
    return f"{v}{suf}"

print(f"{'metric':<22}  {'baseline (dry-run)':<22}  {'treatment (armed)':<22}  delta")
print(f"{'-'*22}  {'-'*22}  {'-'*22}  {'-'*8}")
def _delta(b_v, a_v):
    if b_v is None or a_v is None: return "—"
    if isinstance(b_v, (int, float)):
        d = a_v - b_v
        sign = "+" if d > 0 else ""
        return f"{sign}{d:.1f}"
    return "—"
print(f"{'peak Tctl °C':<22}  {_fmt(b['peak'], '°C'):<22}  {_fmt(a['peak'], '°C'):<22}  {_delta(b['peak'], a['peak'])}")
print(f"{'time ≥85°C (s)':<22}  {_fmt(b['above_85'], 's'):<22}  {_fmt(a['above_85'], 's'):<22}  {_delta(b['above_85'], a['above_85'])}")
print(f"{'first predict (s)':<22}  {_fmt(b['first_predict'], 's'):<22}  {_fmt(a['first_predict'], 's'):<22}  —")
print(f"{'first fan rampup (s)':<22}  {_fmt(b['first_rampup'], 's'):<22}  {_fmt(a['first_rampup'], 's'):<22}  —")
print(f"{'moved-first delta':<22}  {_fmt(b['delta'], 's'):<22}  {_fmt(a['delta'], 's'):<22}  —")
print()
print("Goal: armed peak < baseline peak by ≥3°C; armed above-85 < baseline × 0.5;")
print("      armed delta ≥ 8s positive (coolstep moved first by ≥8 seconds).")
PY

echo ""
echo "==> Both run dirs:"
echo "    baseline: $REPO_ROOT/$RUN_DRY"
echo "    armed:    $REPO_ROOT/$RUN_ARMED"
