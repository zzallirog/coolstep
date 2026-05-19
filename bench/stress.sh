#!/usr/bin/env bash
# bench/stress.sh — coolstep actuator stress-test harness (P2.3)
#
# Usage:  ./bench/stress.sh S1|S2|S3|S4 [--armed|--dry-run] [--duration N]
#
# Writes data/stress-state.json on start (file-based contract for dashboard pill).
# Spawns a background sampler (telemetry.jsonl + ml-state.jsonl, 2s interval).
# On exit (trap EXIT): stops sampler, removes data/stress-state.json.
#
# Metric of record: (t_first_fan_rampup_RPM_4500 - t_first_predict_p_ge_0.65) >= 8s
# Positive delta = coolstep moved first. Computed by bench/analyze.py.
#
# Smoke-test (dry-run, 5 s):
#   ./bench/stress.sh S1 --duration 5
#   Expected: bench/runs/<ts>-S1/ created, 2-3 JSONL lines each, data/stress-state.json removed.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root (the dir containing bench/)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

DASHBOARD_URL="http://127.0.0.1:18889"
DATA_DIR="$REPO_ROOT/data"
BENCH_RUNS="$REPO_ROOT/bench/runs"
STRESS_STATE_FILE="$DATA_DIR/stress-state.json"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SCENARIO=""
ARMED=false
DURATION=""

for arg in "$@"; do
    case "$arg" in
        S1|S2|S3|S4)
            SCENARIO="$arg"
            ;;
        --armed)
            ARMED=true
            ;;
        --dry-run)
            ARMED=false
            ;;
        --duration)
            # next arg is the value — handled below
            ;;
        [0-9]*)
            # numeric arg after --duration
            DURATION="$arg"
            ;;
    esac
done

# Re-parse to catch --duration N properly
_prev=""
for arg in "$@"; do
    if [ "$_prev" = "--duration" ]; then
        DURATION="$arg"
    fi
    _prev="$arg"
done

if [ -z "$SCENARIO" ]; then
    echo "Usage: $0 S1|S2|S3|S4 [--armed|--dry-run] [--duration N]" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Default durations per scenario
# ---------------------------------------------------------------------------
case "$SCENARIO" in
    S1) DEFAULT_DURATION=120 ;;
    S2) DEFAULT_DURATION=90  ;;
    S3) DEFAULT_DURATION=1800 ;;
    S4) DEFAULT_DURATION=300 ;;
esac
DURATION="${DURATION:-$DEFAULT_DURATION}"

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
if ! command -v stress-ng >/dev/null 2>&1; then
    echo "ERROR: stress-ng not found. Install with: sudo pacman -S stress-ng" >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl not found" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Armed-mode warning
# ---------------------------------------------------------------------------
if [ "$ARMED" = true ]; then
    echo "WARNING: --armed mode requested."
    echo "  This exports COOLSTEP_ACTUATOR_ENABLE=true for the duration of the run."
    echo "  The coolstep daemon must be restarted with COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1"
    echo "  to allow actuator firing before the 24h calibration gate."
    echo "  Run: systemctl --user restart coolstep-collector  (after setting the env in the unit)"
    echo ""
    export COOLSTEP_ACTUATOR_ENABLE=true
    ARMED_PY=True
else
    ARMED_PY=False
fi

# ---------------------------------------------------------------------------
# Create run directory
# ---------------------------------------------------------------------------
TS="$(date +%Y%m%dT%H%M%S)"
RUN_DIR="$BENCH_RUNS/${TS}-${SCENARIO}"
mkdir -p "$RUN_DIR"

echo "==> coolstep stress-test harness"
echo "    scenario : $SCENARIO"
echo "    armed    : $ARMED"
echo "    duration : ${DURATION}s"
echo "    run dir  : $RUN_DIR"
echo ""

# ---------------------------------------------------------------------------
# Write data/stress-state.json start marker
# ---------------------------------------------------------------------------
mkdir -p "$DATA_DIR"
python3 -c "
import json, os, time
state = {
    'scenario': '${SCENARIO}',
    'armed': ${ARMED_PY},
    'started_at': time.time(),
    'duration_sec': int('${DURATION}'),
    'pid': os.getpid(),
}
print(json.dumps(state, indent=2))
" > "$STRESS_STATE_FILE"

# ---------------------------------------------------------------------------
# Snapshot Hyprland signature at start (risk §7.5)
# ---------------------------------------------------------------------------
HYPR_SIG_START="$(readlink /run/user/"$UID"/hypr/*/. 2>/dev/null | head -1 || echo unknown)"
python3 -c "
import json, time
meta = {
    'scenario': '${SCENARIO}',
    'armed': ${ARMED_PY},
    'duration_sec': int('${DURATION}'),
    'started_at': time.time(),
    'hypr_sig_start': '${HYPR_SIG_START}',
    'hypr_sig_end': None,
}
with open('${RUN_DIR}/meta.json', 'w') as f:
    json.dump(meta, f, indent=2)
"

# ---------------------------------------------------------------------------
# Background sampler
# ---------------------------------------------------------------------------
SAMPLER_PID=""

_start_sampler() {
    (
        set +e
        while true; do
            NOW="$(python3 -c 'import time; print(time.time())')"

            # Snapshot telemetry/latest
            TEL_JSON="$(curl -sf --max-time 2 "${DASHBOARD_URL}/api/telemetry/latest" 2>/dev/null || echo "")"
            if [ -n "$TEL_JSON" ]; then
                python3 -c "
import json, sys
row = json.loads(sys.stdin.read())
row['recorded_at'] = ${NOW}
print(json.dumps(row))
" <<< "$TEL_JSON" >> "$RUN_DIR/telemetry.jsonl" 2>/dev/null || true
            fi

            # Snapshot ml-state.json
            ML_STATE_PATH="${DATA_DIR}/ml-state.json"
            if [ -f "$ML_STATE_PATH" ]; then
                python3 -c "
import json, sys
with open('${ML_STATE_PATH}') as f:
    row = json.load(f)
row['recorded_at'] = ${NOW}
print(json.dumps(row))
" >> "$RUN_DIR/ml-state.jsonl" 2>/dev/null || true
            fi

            sleep 2
        done
    ) &
    SAMPLER_PID=$!
}

_stop_sampler() {
    if [ -n "$SAMPLER_PID" ] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
        kill "$SAMPLER_PID" 2>/dev/null || true
        wait "$SAMPLER_PID" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Cleanup trap (EXIT — covers normal exit, SIGINT, SIGTERM)
# ---------------------------------------------------------------------------
_cleanup() {
    local exit_code=$?
    _stop_sampler

    # Unset armed env
    if [ "$ARMED" = true ]; then
        unset COOLSTEP_ACTUATOR_ENABLE
    fi

    # Snapshot Hyprland signature at end
    HYPR_SIG_END="$(readlink /run/user/"$UID"/hypr/*/. 2>/dev/null | head -1 || echo unknown)"
    python3 -c "
import json
with open('${RUN_DIR}/meta.json') as f:
    meta = json.load(f)
meta['hypr_sig_end'] = '${HYPR_SIG_END}'
with open('${RUN_DIR}/meta.json', 'w') as f:
    json.dump(meta, f, indent=2)
" 2>/dev/null || true

    # Remove stress-state marker
    rm -f "$STRESS_STATE_FILE"

    echo ""
    echo "==> run dir: $RUN_DIR"
    if [ "$exit_code" -eq 0 ]; then
        echo "    status: completed"
    else
        echo "    status: interrupted (exit $exit_code)"
    fi
    echo "    analyze: python3 bench/analyze.py '$RUN_DIR'"
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Start sampler
# ---------------------------------------------------------------------------
_start_sampler
echo "Sampler PID: $SAMPLER_PID"
echo ""

# ---------------------------------------------------------------------------
# Run scenario
# ---------------------------------------------------------------------------
case "$SCENARIO" in
    S1)
        echo "S1: Synthetic compute burst — 16 threads × 100% CPU for ${DURATION}s"
        stress-ng --cpu 16 --cpu-load 100 --timeout "${DURATION}s" --metrics
        ;;
    S2)
        echo "S2: Mixed compute + iowait — 8 CPU + 4 HDD workers for ${DURATION}s"
        stress-ng --cpu 8 --hdd 4 --timeout "${DURATION}s" --metrics
        ;;
    S3)
        echo "S3: Heat-soak — 16 threads × 80% CPU for ${DURATION}s (default 30 min)"
        stress-ng --cpu 16 --cpu-load 80 --timeout "${DURATION}s" --metrics
        ;;
    S4)
        echo "S4: User-driven load (no synthetic stressor)."
        echo "    Launch Steam, a game, or a heavy workload NOW."
        echo "    Sampler is active. Harness will exit in ${DURATION}s."
        echo "    Press Ctrl+C to stop early."
        sleep "$DURATION"
        ;;
esac
