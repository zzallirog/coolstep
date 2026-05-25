#!/usr/bin/env bash
# Install coolstep's git hooks (pre-commit + pre-push). Idempotent —
# re-runs replace the symlinks.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

if ! [ -d "$HOOKS_DIR" ]; then
    echo "ERROR: $HOOKS_DIR not found — is this a git checkout?" >&2
    exit 1
fi

mkdir -p "$HOOKS_DIR"

# Symlink instead of copy so updates to the scripts are picked up live.
for hook in pre-commit pre-push; do
    src="$REPO_ROOT/scripts/${hook}.sh"
    dst="$HOOKS_DIR/$hook"
    if [ -e "$src" ]; then
        ln -sf "$src" "$dst"
        chmod +x "$dst"
        echo "installed: $dst → $src"
    fi
done
