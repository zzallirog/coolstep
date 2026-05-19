#!/usr/bin/env bash
# coolstep-cleanup — belt-and-braces revert for SIGKILL / OOM.
#
# Invoked from `ExecStopPost=` on coolstep-collector. ONLY touches hardware
# if `runtime-state.json` shows the daemon had armed actions when it died.
# Preserves user-tuned curves by preferring the persisted baseline snapshot
# (`asusctl_fan_curve_baseline.json`) over a factory `--default` reset.
#
# Restore hierarchy:
#   1. armed_actions empty   → no-op (clean shutdown / never armed)
#   2. armed non-empty + baseline file exists  → re-apply baseline via --data
#                                                 (preserves user's curve)
#   3. armed non-empty + no baseline           → factory --default (last resort)

set -u

state_file="${COOLSTEP_HOME:-$HOME/coolstep/data}/runtime-state.json"
baseline_file="${COOLSTEP_HOME:-$HOME/coolstep/data}/asusctl_fan_curve_baseline.json"

# No state file → nothing to revert (fresh install / first run).
if [ ! -f "$state_file" ]; then
    exit 0
fi

# Count armed entries via python (jq isn't a required dep on the host).
armed_count=$(python3 -c "
import json
try:
    d = json.load(open('$state_file'))
    print(len(d.get('armed_actions') or []))
except Exception:
    print(0)
" 2>/dev/null || echo 0)

if [ "$armed_count" = "0" ]; then
    # Clean exit — daemon's Python finally already reverted, or never armed.
    exit 0
fi

# Armed actions remained on disk → daemon died hard. Try baseline restore first.
profile="${COOLSTEP_ASUSCTL_PROFILE:-Performance}"
if ! command -v asusctl >/dev/null 2>&1; then
    exit 0
fi

if [ -f "$baseline_file" ]; then
    # Build asusctl --data string from baseline anchors (sorted by temp).
    data_arg=$(python3 -c "
import json
try:
    b = json.load(open('$baseline_file'))
    anchors = sorted(b.get('anchors') or [], key=lambda a: a[0])
    print(','.join(f'{int(round(t))}c:{int(round(p))}%' for t, p in anchors))
except Exception:
    print('')
" 2>/dev/null || echo "")
    fan=$(python3 -c "
import json
try:
    print(json.load(open('$baseline_file')).get('fan') or 'cpu')
except Exception:
    print('cpu')
" 2>/dev/null || echo "cpu")
    if [ -n "$data_arg" ]; then
        asusctl fan-curve --mod-profile "$profile" --fan "$fan" --data "$data_arg" >/dev/null 2>&1 || true
        kind="baseline_restore"
    else
        # baseline file unreadable → factory fallback
        asusctl fan-curve --mod-profile "$profile" --default >/dev/null 2>&1 || true
        kind="factory_default"
    fi
else
    # No baseline saved → factory fallback (true last-resort)
    asusctl fan-curve --mod-profile "$profile" --default >/dev/null 2>&1 || true
    kind="factory_default"
fi

# Record crash-recovery event for the dashboard (best-effort).
python3 -c "
import json, time
with open('${state_file%/*}/last-crash-recovery.json', 'w') as f:
    json.dump({'ts': time.time(), 'kind': '$kind', 'armed_count': $armed_count}, f)
" 2>/dev/null || true

# Clear armed_actions so next start doesn't loop.
python3 -c "
import json
with open('$state_file') as f:
    d = json.load(f)
d['armed_actions'] = []
with open('$state_file', 'w') as f:
    json.dump(d, f)
" 2>/dev/null || true
