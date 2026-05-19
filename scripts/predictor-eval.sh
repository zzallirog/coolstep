#!/usr/bin/env bash
# scripts/predictor-eval.sh — eval the meta-predictor's accuracy from
# the residual log.  Composes coolstep CLI subcommands; no in-script
# Python math.
#
# Usage:
#   scripts/predictor-eval.sh                    # default: last 1h
#   scripts/predictor-eval.sh 24h
#   scripts/predictor-eval.sh 7d --json
#
# Bash-over-Python by design: bash for stitching, Python for the math.

set -euo pipefail

WINDOW="${1:-1h}"
JSON_FLAG=""
if [ "${2:-}" = "--json" ]; then
    JSON_FLAG="--as-json"
fi

ANSI_BOLD=$'\033[1m'
ANSI_DIM=$'\033[2m'
ANSI_OK=$'\033[32m'
ANSI_WARN=$'\033[33m'
ANSI_ERR=$'\033[31m'
ANSI_RESET=$'\033[0m'

color() {
    # color <kind> <text> — emit colored text if stdout is a tty.
    local kind="$1"; shift
    if [ -t 1 ]; then
        case "$kind" in
            bold) printf '%s' "${ANSI_BOLD}$*${ANSI_RESET}" ;;
            dim)  printf '%s' "${ANSI_DIM}$*${ANSI_RESET}" ;;
            ok)   printf '%s' "${ANSI_OK}$*${ANSI_RESET}" ;;
            warn) printf '%s' "${ANSI_WARN}$*${ANSI_RESET}" ;;
            err)  printf '%s' "${ANSI_ERR}$*${ANSI_RESET}" ;;
        esac
    else
        printf '%s' "$*"
    fi
}

if ! command -v coolstep >/dev/null 2>&1; then
    echo "coolstep CLI not in PATH — activate the venv or install the package" >&2
    exit 1
fi

LOG_PATH="${COOLSTEP_HOME:-$HOME/coolstep/data}/residual-state.jsonl"
if [ ! -f "$LOG_PATH" ]; then
    echo "no residual log at $LOG_PATH — has the collector been running?" >&2
    exit 2
fi

color bold "coolstep predictor evaluation"
echo " · window=$WINDOW"
echo

# Section 1: overall accuracy from the dashboard endpoint (if reachable).
if curl -sS --max-time 1 -o /tmp/cs-cockpit.json -w "%{http_code}" \
    http://127.0.0.1:18889/api/predictor-cockpit >/dev/null 2>&1; then
    code=$?
    if [ "$code" = "0" ] && [ -s /tmp/cs-cockpit.json ]; then
        ACC=$(jq -r '.accuracy_pct // 0' /tmp/cs-cockpit.json)
        MED=$(jq -r '.median_abs_err_c // "n/a"' /tmp/cs-cockpit.json)
        BUCKETS=$(jq -r '.meta_buckets // 0' /tmp/cs-cockpit.json)
        LOGCNT=$(jq -r '.residual_log_count // 0' /tmp/cs-cockpit.json)
        PROFILE=$(jq -r '.active_tuned_profile // "(none)"' /tmp/cs-cockpit.json)
        ACC_PCT=$(awk -v a="$ACC" 'BEGIN{printf "%.0f%%", a*100}')

        # Color by threshold.
        if awk "BEGIN{exit !($ACC >= 0.85)}"; then
            ACC_KIND="ok"
        elif awk "BEGIN{exit !($ACC >= 0.65)}"; then
            ACC_KIND="warn"
        else
            ACC_KIND="err"
        fi

        printf "  live (last 15m)\n"
        printf "    accuracy:       %s   (median |err| %s°C)\n" \
            "$(color "$ACC_KIND" "$ACC_PCT")" "$MED"
        printf "    active profile: %s\n" "$(color bold "$PROFILE")"
        printf "    buckets:        %s   log entries: %s\n" "$BUCKETS" "$LOGCNT"
        echo
    fi
fi

# Section 2: window replay via CLI.
color bold "per-bucket replay"
echo " · window=$WINDOW"
echo
if [ -n "$JSON_FLAG" ]; then
    coolstep predict-replay --since "$WINDOW" $JSON_FLAG
else
    coolstep predict-replay --since "$WINDOW" || true
fi

echo
color dim "  bucket key = (load_band, slope_sign, accel_sign, profile_band)"
echo
color dim "    load_band:    -1 falling  | 0 flat | +1 rising"
echo
color dim "    slope_sign:   -1 cooling  | 0 flat | +1 heating"
echo
color dim "    accel_sign:   -1 decel    | 0 lin  | +1 accel"
echo
color dim "    profile_band:  0 <60°C    | 1 60-80°C | 2 ≥80°C"
echo
