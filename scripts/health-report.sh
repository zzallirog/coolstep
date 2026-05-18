#!/usr/bin/env bash
# scripts/health-report.sh — capture coolstep state for diagnostics.
#
# Output: /tmp/coolstep-health-<unix-ts>.txt
# Also prints a short ✓/✗ summary on stdout.
#
# Designed to be safe to run anytime: read-only commands only, no daemon kicks.

set -uo pipefail  # no -e: collect data even if some commands fail

TS=$(date +%s)
OUT="/tmp/coolstep-health-${TS}.txt"
COOLSTEP_HOME="${COOLSTEP_HOME:-$HOME/coolstep/data}"

echo "==> coolstep health snapshot $(date -Iseconds)" | tee "$OUT"
echo "    home: $COOLSTEP_HOME" | tee -a "$OUT"
echo "    out:  $OUT" | tee -a "$OUT"
echo "" >> "$OUT"

# Helper to section headers
_section() {
    echo "" >> "$OUT"
    echo "=== $1 ===" >> "$OUT"
}

# 1. systemctl status
_section "systemctl --user is-active"
for unit in coolstep-collector.service coolstep-dashboard.service coolstep-bench-gc.timer; do
    if systemctl --user list-unit-files "$unit" >/dev/null 2>&1; then
        state=$(systemctl --user is-active "$unit" 2>&1 || true)
        echo "  $unit: $state" >> "$OUT"
    else
        echo "  $unit: (not installed)" >> "$OUT"
    fi
done

# 2. /api/health
_section "/api/health"
if curl -sf --max-time 3 http://127.0.0.1:18889/api/health > /tmp/coolstep-health-api.json 2>&1; then
    cat /tmp/coolstep-health-api.json >> "$OUT"
    rm -f /tmp/coolstep-health-api.json
else
    echo "  (dashboard unreachable)" >> "$OUT"
fi

# 3. /api/adapters
_section "/api/adapters"
curl -s --max-time 3 http://127.0.0.1:18889/api/adapters 2>&1 | python3 -m json.tool 2>&1 >> "$OUT" || echo "  (failed)" >> "$OUT"

# 4. /api/calibration
_section "/api/calibration"
curl -s --max-time 3 http://127.0.0.1:18889/api/calibration 2>&1 | python3 -m json.tool 2>&1 >> "$OUT" || echo "  (failed)" >> "$OUT"

# 5. /api/crash-recovery
_section "/api/crash-recovery"
curl -s --max-time 3 http://127.0.0.1:18889/api/crash-recovery 2>&1 >> "$OUT" || echo "  (failed)" >> "$OUT"
echo "" >> "$OUT"

# 6. Daemon journal tail
_section "journalctl --user -u coolstep-collector (last 50)"
journalctl --user -u coolstep-collector -n 50 --no-pager >> "$OUT" 2>&1 || echo "  (failed)" >> "$OUT"

# 7. Dashboard journal tail
_section "journalctl --user -u coolstep-dashboard (last 30)"
journalctl --user -u coolstep-dashboard -n 30 --no-pager >> "$OUT" 2>&1 || echo "  (failed)" >> "$OUT"

# 8. cgroup memory.events
_section "cgroup memory.events (coolstep-collector)"
CG="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service"
# Find the slice — try common paths. Override with COOLSTEP_SLICE=<slice-name>
# if the unit was pinned to a custom slice via a drop-in.
SLICE="${COOLSTEP_SLICE:-}"
candidates=(
    "$CG/coolstep-collector.service"
    "$CG/app.slice/coolstep-collector.service"
)
if [ -n "$SLICE" ]; then
    candidates=("$CG/$SLICE/coolstep-collector.service" "${candidates[@]}")
fi
for candidate in "${candidates[@]}"; do
    if [ -d "$candidate" ]; then
        echo "  path: $candidate" >> "$OUT"
        cat "$candidate/memory.events" >> "$OUT" 2>&1
        echo "" >> "$OUT"
        echo "  current RSS: $(cat "$candidate/memory.current" 2>/dev/null || echo "?")B" >> "$OUT"
        break
    fi
done

# 9. Files in COOLSTEP_HOME
_section "$COOLSTEP_HOME/ contents"
ls -la "$COOLSTEP_HOME" 2>&1 >> "$OUT" || echo "  (home dir missing)" >> "$OUT"

# 10. Journal jsonl size
_section "actuator-journal.jsonl size"
JNL="$COOLSTEP_HOME/actuator-journal.jsonl"
if [ -f "$JNL" ]; then
    size=$(stat -c %s "$JNL")
    lines=$(wc -l < "$JNL")
    echo "  size: ${size}B" >> "$OUT"
    echo "  lines: ${lines}" >> "$OUT"
    # Rotation generations
    for gen in "${JNL}.1" "${JNL}.2"; do
        [ -f "$gen" ] && echo "  $(basename "$gen"): $(stat -c %s "$gen")B" >> "$OUT"
    done
else
    echo "  (no journal yet)" >> "$OUT"
fi

# 11. Baseline file
_section "asusctl_fan_curve_baseline.json"
BASE="$COOLSTEP_HOME/asusctl_fan_curve_baseline.json"
if [ -f "$BASE" ]; then
    cat "$BASE" | python3 -m json.tool 2>&1 >> "$OUT" || cat "$BASE" >> "$OUT"
else
    echo "  (no baseline snapshot — actuator hasn't fired yet)" >> "$OUT"
fi

# 12. Current asusctl fan curve (live)
_section "asusctl fan-curve --mod-profile Performance (live)"
if command -v asusctl >/dev/null 2>&1; then
    asusctl fan-curve --mod-profile Performance 2>&1 >> "$OUT"
else
    echo "  (asusctl not installed)" >> "$OUT"
fi

# Stdout summary
echo ""
echo "==> Summary (full report in $OUT):"

# Quick status indicators
collector_state=$(systemctl --user is-active coolstep-collector.service 2>&1 || true)
dashboard_state=$(systemctl --user is-active coolstep-dashboard.service 2>&1 || true)
api_ok=$(curl -sf --max-time 2 http://127.0.0.1:18889/api/health >/dev/null 2>&1 && echo "✓" || echo "✗")
journal_lines="—"
[ -f "$JNL" ] && journal_lines=$(wc -l < "$JNL")
baseline_status="—"
[ -f "$BASE" ] && baseline_status="present"

echo "  collector:       $collector_state"
echo "  dashboard:       $dashboard_state"
echo "  /api/health:     $api_ok"
echo "  journal lines:   $journal_lines"
echo "  baseline file:   $baseline_status"
echo ""
echo "  Full report: $OUT"
