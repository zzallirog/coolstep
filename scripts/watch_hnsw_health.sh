#!/bin/bash
# watch_hnsw_health.sh — poll /api/predictor-cockpit after hnsw backend flip
# Prints tab-separated rows; emits p50/p95 summary and HEALTHY/DEGRADED/STALE verdict.
# Usage: watch_hnsw_health.sh [--duration N] [--interval N] [--once]

set -euo pipefail

BASE_URL="http://127.0.0.1:18889"
DURATION=60
INTERVAL=2
ONCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --duration)
            DURATION="$2"; shift 2 ;;
        --interval)
            INTERVAL="$2"; shift 2 ;;
        --once)
            ONCE=1; shift ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

fetch_cockpit() {
    curl -sf --max-time 4 "${BASE_URL}/api/predictor-cockpit" 2>/dev/null
}

fetch_health() {
    curl -sf --max-time 4 "${BASE_URL}/api/health" 2>/dev/null
}

get_field() {
    printf '%s' "$1" | jq -r "$2 // \"null\""
}

printf '%s\t%s\t%s\t%s\n' "time" "refresh_last_ms" "refresh_skipped" "prediction_age_sec"

declare -a refresh_samples=()
skipped_first=""
skipped_last=""
elapsed=0

poll_once() {
    local ts
    ts="$(date +%H:%M:%S)"

    local body
    body="$(fetch_cockpit)" || body=""

    if [ -z "$body" ]; then
        health="$(fetch_health)" || health=""
        if [ -z "$health" ]; then
            printf '%s\tERROR: daemon unreachable\n' "$ts"
        else
            printf '%s\tERROR: /api/predictor-cockpit missing (health OK)\n' "$ts"
        fi
        return
    fi

    local r_ms skipped age
    r_ms="$(get_field "$body" '.predict_refresh_last_ms')"
    skipped="$(get_field "$body" '.predict_refresh_skipped')"
    age="$(get_field "$body" '.prediction_age_sec')"

    printf '%s\t%s\t%s\t%s\n' "$ts" "$r_ms" "$skipped" "$age"

    if [ "$r_ms" != "null" ]; then
        refresh_samples+=("$r_ms")
    fi
    if [ "$skipped" != "null" ]; then
        if [ -z "$skipped_first" ]; then
            skipped_first="$skipped"
        fi
        skipped_last="$skipped"
    fi
}

poll_once

if [ "$ONCE" = "1" ]; then
    exit 0
fi

while [ "$elapsed" -lt "$DURATION" ]; do
    sleep "$INTERVAL"
    elapsed=$(( elapsed + INTERVAL ))
    poll_once
done

# --- summary ---
count="${#refresh_samples[@]}"
if [ "$count" -eq 0 ]; then
    echo ""
    echo "SUMMARY: no valid samples collected"
    exit 2
fi

# sort numerically via sort
sorted_samples="$(printf '%s\n' "${refresh_samples[@]}" | sort -n)"
p50_idx=$(( (count - 1) / 2 ))
p95_idx=$(( (count * 95) / 100 ))
[ "$p95_idx" -ge "$count" ] && p95_idx=$(( count - 1 ))

p50="$(printf '%s\n' "${refresh_samples[@]}" | sort -n | sed -n "$(( p50_idx + 1 ))p")"
p95="$(printf '%s\n' "${refresh_samples[@]}" | sort -n | sed -n "$(( p95_idx + 1 ))p")"

skipped_delta=0
if [ -n "$skipped_first" ] && [ -n "$skipped_last" ]; then
    skipped_delta=$(( skipped_last - skipped_first ))
fi

echo ""
printf 'SUMMARY  samples=%d  p50_refresh_ms=%s  p95_refresh_ms=%s  skipped_delta=%d\n' \
    "$count" "$p50" "$p95" "$skipped_delta"

verdict=""
exit_code=0
# Use awk for float comparison (jq values may be floats)
p95_int="$(printf '%s' "$p95" | awk '{printf "%d", int($1 + 0.5)}')"
if [ "$p95_int" -lt 100 ] && [ "$skipped_delta" -lt 5 ]; then
    verdict="HEALTHY"
elif [ "$p95_int" -lt 1000 ]; then
    verdict="DEGRADED"
else
    verdict="STALE / CHROMA-LIKE"
    exit_code=2
fi

printf 'VERDICT  %s\n' "$verdict"
exit "$exit_code"
