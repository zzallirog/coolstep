#!/usr/bin/env bash
# Pre-push gate. Currently runs CLAUDE.md drift check.
#
# Behaviour:
#   * Structural drift (table mismatch, missing routes, version skew) blocks push.
#   * Stamp-only drift is auto-fixed in-place via `--bump-stamps` and the bumped
#     files are added to a fixup commit so the push doesn't carry stale dates.
#   * To bypass on purpose: `git push --no-verify`.
#
# Install: ./scripts/install-hooks.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECKER="$REPO_ROOT/scripts/claude-md-drift-check.py"

if ! [ -x "$CHECKER" ]; then
    echo "pre-push: $CHECKER not executable, skipping drift check" >&2
    exit 0
fi

cd "$REPO_ROOT"

# First pass: bump stamps where the only complaint is a stale date.
"$CHECKER" --bump-stamps --quiet >/tmp/coolstep-claude-drift.out 2>&1 || true

# Now re-run to capture the *real* exit code post-autofix.
if ! "$CHECKER" --quiet; then
    echo "pre-push: structural CLAUDE.md drift detected — push blocked." >&2
    echo "          run \`python3 scripts/claude-md-drift-check.py\` to see details," >&2
    echo "          fix the tables, commit, and re-push." >&2
    exit 1
fi

# If --bump-stamps wrote anything that's tracked, surface it so the user
# can decide whether to amend the last commit before pushing.
if ! git diff --quiet -- '**/CLAUDE.md'; then
    echo "pre-push: stamps bumped in CLAUDE.md children. Stage + amend if you want" >&2
    echo "          them on this push, or push without and they ride the next commit." >&2
    git status -s -- '**/CLAUDE.md' >&2
fi

exit 0
