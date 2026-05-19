#!/usr/bin/env bash
# coolstep/scripts/quickstart.sh — copy systemd units, enable + start daemon + verify.
#
# Usage:
#   scripts/quickstart.sh          # full install + start
#   scripts/quickstart.sh --dry-run  # show what would happen
#   scripts/quickstart.sh --no-start  # install only, don't start
#
# Idempotent: re-runs are safe (cp -f, systemctl restart already-running services).

set -euo pipefail

DRY_RUN=false
START=true
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY_RUN=true ;;
        --no-start) START=false ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^#//' | head -20
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_SRC="$REPO_ROOT/systemd"
SYSTEMD_DST="$HOME/.config/systemd/user"

_run() {
    if [ "$DRY_RUN" = true ]; then
        echo "  DRY-RUN: $*"
    else
        echo "  + $*"
        "$@"
    fi
}

echo "==> coolstep quickstart"
echo "    repo:    $REPO_ROOT"
echo "    systemd: $SYSTEMD_DST"
echo "    dry-run: $DRY_RUN"
echo ""

# 1. Sanity: do we even have the units?
if ! [ -d "$SYSTEMD_SRC" ]; then
    echo "ERROR: $SYSTEMD_SRC not found — wrong repo?" >&2
    exit 1
fi

# 2. Ensure target dir exists
echo "--- (1/5) systemd user dir ---"
_run mkdir -p "$SYSTEMD_DST"

# 3. Copy unit files (.service + .timer)
echo "--- (2/5) copy unit files ---"
copied=0
for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
    [ -f "$unit" ] || continue
    _run cp -f "$unit" "$SYSTEMD_DST/$(basename "$unit")"
    copied=$((copied + 1))
done
echo "  $copied unit file(s) copied"

# 4. daemon-reload
echo "--- (3/5) systemctl --user daemon-reload ---"
_run systemctl --user daemon-reload

# 5. Enable + (optionally) start
echo "--- (4/5) enable units ---"
for unit in coolstep-collector.service coolstep-dashboard.service; do
    if [ -f "$SYSTEMD_DST/$unit" ]; then
        _run systemctl --user enable "$unit"
    fi
done

if [ "$START" = true ]; then
    echo "--- (5/5) start units + verify ---"
    for unit in coolstep-collector.service coolstep-dashboard.service; do
        if [ -f "$SYSTEMD_DST/$unit" ]; then
            _run systemctl --user restart "$unit"
        fi
    done

    # Brief wait + smoke check
    if [ "$DRY_RUN" = false ]; then
        sleep 4
        echo ""
        echo "==> smoke /api/health"
        if curl -sf --max-time 5 http://127.0.0.1:18889/api/health > /tmp/coolstep-quickstart-health.json; then
            cat /tmp/coolstep-quickstart-health.json
            echo ""
            echo ""
            echo "==> OK — dashboard alive at http://127.0.0.1:18889/"
        else
            echo "WARNING: /api/health did not respond within 5s"
            echo "Check: journalctl --user -u coolstep-dashboard -n 20"
        fi
    fi
else
    echo "--- (5/5) skipped (--no-start) ---"
fi

# 6. Next steps
echo ""
echo "==> Next steps:"
echo "  Dry-run mode (default, safe): nothing more — daemon predicts + logs intent."
echo ""
echo "  To ARM the bias actuator (real fan-curve writes):"
echo "    mkdir -p ~/.config/systemd/user/coolstep-collector.service.d"
echo "    cat > ~/.config/systemd/user/coolstep-collector.service.d/50-armed-prod.conf <<EOF"
echo "    [Service]"
echo "    Environment=COOLSTEP_ACTUATOR_ENABLE=true"
echo "    Environment=COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1"
echo "    EOF"
echo "    systemctl --user daemon-reload"
echo "    systemctl --user restart coolstep-collector"
echo ""
echo "  Smoke stress test:"
echo "    bench/stress.sh S1 --duration 30 --dry-run"
