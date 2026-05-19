#!/bin/bash
# hnsw_rollback.sh — roll back hnsw drop-in by renaming it to .disabled
# then daemon-reload + restart coolstep-collector.service (user systemd).
# Usage: hnsw_rollback.sh [--dry-run] [--force]

set -euo pipefail

DROPIN_ACTIVE="${HOME}/.config/systemd/user/coolstep-collector.service.d/80-knn-hnsw.conf"
DROPIN_DISABLED="${DROPIN_ACTIVE}.disabled"
DRY_RUN=0
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1; shift ;;
        --force)
            FORCE=1; shift ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# Pre-check
if [ ! -f "$DROPIN_ACTIVE" ]; then
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

echo "Renaming drop-in to .disabled..."
mv "$DROPIN_ACTIVE" "$DROPIN_DISABLED"

echo "Running daemon-reload..."
systemctl --user daemon-reload

echo "Restarting coolstep-collector.service..."
systemctl --user restart coolstep-collector.service

echo "Rollback complete. Drop-in disabled: $DROPIN_DISABLED"
