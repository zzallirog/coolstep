#!/usr/bin/env bash
# Install coolstep's pre-commit hook into .git/hooks/pre-commit.
# Idempotent: re-runs replace the symlink.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

if ! [ -d "$HOOKS_DIR" ]; then
    echo "ERROR: $HOOKS_DIR not found — is this a git checkout?" >&2
    exit 1
fi

mkdir -p "$HOOKS_DIR"
# Symlink instead of copy so updates to scripts/pre-commit.sh are picked up live.
ln -sf "$REPO_ROOT/scripts/pre-commit.sh" "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"
echo "installed: $HOOKS_DIR/pre-commit → $REPO_ROOT/scripts/pre-commit.sh"
