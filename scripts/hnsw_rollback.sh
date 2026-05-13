#!/bin/bash
# hnsw_rollback.sh — roll back hnsw drop-in by renaming it to .disabled
# then daemon-reload + restart coolstep-collector.service (user systemd).
# Optionally restore the most-recent data/hnsw.bak-* dir created by
# cutover_to_hnsw.sh via --restore-data.
# Usage: hnsw_rollback.sh [--dry-run] [--force] [--restore-data]

set -euo pipefail

DROPIN_ACTIVE="${HOME}/.config/systemd/user/coolstep-collector.service.d/80-knn-hnsw.conf"
DROPIN_DISABLED="${DROPIN_ACTIVE}.disabled"
HNSW_DATA="${HOME}/coolstep/data/hnsw"
DRY_RUN=0
FORCE=0
RESTORE_DATA=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1; shift ;;
        --force)
            FORCE=1; shift ;;
        --restore-data)
            RESTORE_DATA=1; shift ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# Pre-check
if [ ! -f "$DROPIN_ACTIVE" ] && [ "$RESTORE_DATA" != "1" ]; then
    echo "Drop-in not active (already rolled back or never enabled): $DROPIN_ACTIVE"
    exit 0
fi

echo "Active drop-in found: $DROPIN_ACTIVE"

if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] Would rename: $DROPIN_ACTIVE -> $DROPIN_DISABLED"
    echo "[dry-run] Would run: systemctl --user daemon-reload"
    echo "[dry-run] Would run: systemctl --user restart coolstep-collector.service"
    exit 0
fi

if [ "$FORCE" != "1" ]; then
    printf 'Roll back hnsw drop-in and restart coolstep-collector? [y/N] '
    read -r answer
    case "$answer" in
        y|Y) ;;
        *)
            echo "Aborted."
            exit 0
            ;;
    esac
fi

if [ -f "$DROPIN_ACTIVE" ]; then
    echo "Renaming drop-in to .disabled..."
    mv "$DROPIN_ACTIVE" "$DROPIN_DISABLED"
fi

# Optional data restore: pick the newest data/hnsw.bak-* from
# cutover_to_hnsw.sh and put it back in place. Useful when rolling back
# because of HNSW data corruption (where rolling back only the drop-in
# leaves the daemon falling back to a still-corrupt directory).
if [ "$RESTORE_DATA" = "1" ]; then
    NEWEST_BAK="$(ls -1dt "${HNSW_DATA}.bak-"* 2>/dev/null | head -n 1 || true)"
    if [ -z "$NEWEST_BAK" ]; then
        echo "WARNING: --restore-data requested but no ${HNSW_DATA}.bak-* found" >&2
    else
        echo "Restoring HNSW data from: $NEWEST_BAK"
        STAMP="$(date +%Y%m%d-%H%M%S)"
        if [ -d "$HNSW_DATA" ]; then
            mv "$HNSW_DATA" "${HNSW_DATA}.failed-${STAMP}"
        fi
        mv "$NEWEST_BAK" "$HNSW_DATA"
    fi
fi

echo "Running daemon-reload..."
systemctl --user daemon-reload

echo "Restarting coolstep-collector.service..."
systemctl --user restart coolstep-collector.service

echo "Rollback complete."
