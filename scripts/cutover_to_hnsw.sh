#!/bin/bash
# cutover_to_hnsw.sh — orchestrate the Phase 1 → Phase 2 flip in one shot.
# Stops collector, migrates 42k chroma vectors → hnsw, enables drop-in,
# reloads + restarts daemon, then runs watch_hnsw_health.sh for 60s.
# Pairs with hnsw_rollback.sh as the documented rollback path.

set -euo pipefail

REPO="${HOME}/coolstep"
VENV_PY="${REPO}/.venv/bin/python"
DROPIN_DIR="${HOME}/.config/systemd/user/coolstep-collector.service.d"
DROPIN_ACTIVE="${DROPIN_DIR}/80-knn-hnsw.conf"
DROPIN_STAGED="${DROPIN_ACTIVE}.disabled"
WATCH="${REPO}/scripts/watch_hnsw_health.sh"
MIGRATE="${REPO}/scripts/reindex_hnsw_from_store.py"
HNSW_DATA="${REPO}/data/hnsw"

DRY_RUN=0
SKIP_MIGRATE=0
WATCH_DURATION=60
HNSW_BACKUP=""   # set when we rename live → .bak-STAMP, consumed by trap

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1; shift ;;
        --skip-migrate)
            SKIP_MIGRATE=1; shift ;;
        --duration)
            if [ $# -lt 2 ]; then
                echo "ERROR: --duration requires a value" >&2; exit 1
            fi
            WATCH_DURATION="$2"; shift 2 ;;
        *)
            echo "Unknown arg: $1" >&2
            echo "Usage: $0 [--dry-run] [--skip-migrate] [--duration N]" >&2
            exit 1 ;;
    esac
done

# Rollback: if migration (or any step before drop-in flip) fails, restore
# the backed-up live dir. Without this trap, an interrupted run leaves
# data/hnsw/ either missing or half-written, and hnsw_rollback.sh only
# touches the drop-in — not the data. ERR fires on `set -e` triggers,
# EXIT on any exit; success path nulls HNSW_BACKUP before its own trap-clear.
_rollback_on_failure() {
    local rc=$?
    if [ -n "$HNSW_BACKUP" ] && [ -d "$HNSW_BACKUP" ]; then
        echo "*** cutover failed (rc=$rc) — restoring HNSW backup: $HNSW_BACKUP" >&2
        rm -rf "$HNSW_DATA"
        mv "$HNSW_BACKUP" "$HNSW_DATA"
    fi
}
trap _rollback_on_failure ERR

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] $*"
    else
        echo "+ $*"
        "$@"
    fi
}

# Preflight
if [ ! -f "$DROPIN_STAGED" ]; then
    echo "ERROR: staged drop-in not found: $DROPIN_STAGED" >&2
    echo "Agent B must have written 80-knn-hnsw.conf.disabled before cutover." >&2
    exit 2
fi
if [ -f "$DROPIN_ACTIVE" ]; then
    echo "ERROR: active drop-in already exists: $DROPIN_ACTIVE" >&2
    echo "Run hnsw_rollback.sh first or remove manually." >&2
    exit 2
fi
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: venv python missing: $VENV_PY" >&2
    exit 2
fi
if [ ! -f "$MIGRATE" ]; then
    echo "ERROR: migrate script missing: $MIGRATE" >&2
    exit 2
fi

echo "=== Cutover plan ==="
echo "  1. Stop coolstep-collector.service"
[ "$SKIP_MIGRATE" = "1" ] && echo "  2. SKIP migrate (--skip-migrate)" || echo "  2. Migrate 42k chroma → hnsw"
echo "  3. Enable drop-in: 80-knn-hnsw.conf{.disabled,}"
echo "  4. daemon-reload + restart coolstep-collector"
echo "  5. Watch for ${WATCH_DURATION}s"
echo

run systemctl --user stop coolstep-collector.service

if [ "$SKIP_MIGRATE" != "1" ]; then
    # Archive any stale on-disk hnsw state (rather than rm -rf — keeps a
    # rollback option). Migration via chromadb is unreliable (mixed hnswlib
    # versions corrupt the chroma index file); we replay store.db through
    # the same Embedder instead — bit-identical vectors.
    if [ -d "$HNSW_DATA" ]; then
        STAMP="$(date +%Y%m%d-%H%M%S)"
        HNSW_BACKUP="${HNSW_DATA}.bak-${STAMP}"
        run mv "$HNSW_DATA" "$HNSW_BACKUP"
    fi
    run "$VENV_PY" "$MIGRATE"
fi

# Past the destructive window — clear rollback target so success doesn't
# accidentally restore an older snapshot on a later (recoverable) error.
HNSW_BACKUP=""

run mv "$DROPIN_STAGED" "$DROPIN_ACTIVE"
run systemctl --user daemon-reload
run systemctl --user start coolstep-collector.service

if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] Would run: $WATCH --duration $WATCH_DURATION"
    exit 0
fi

echo "Waiting 5s for daemon to seed prediction cache..."
sleep 5

echo "=== Watch ==="
"$WATCH" --duration "$WATCH_DURATION"
