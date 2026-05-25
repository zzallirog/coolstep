#!/usr/bin/env bash
# coolstep pre-commit checks — ruff + pytest fast subset.
# Run manually: scripts/pre-commit.sh
# Installed by scripts/install-hooks.sh as .git/hooks/pre-commit (symlink).

set -euo pipefail

# Resolve REPO_ROOT through the symlink — when this script lives at
# .git/hooks/pre-commit (a symlink), `$0` would point inside .git/ and
# `dirname $0/..` would land at the .git directory itself, not the repo.
# `readlink -f` on $BASH_SOURCE follows the chain to the real script.
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> pre-commit: ruff check"
if command -v ruff >/dev/null 2>&1; then
    ruff check coolstep/ tests/ || exit 1
elif [ -x .venv/bin/ruff ]; then
    .venv/bin/ruff check coolstep/ tests/ || exit 1
else
    echo "  (ruff not found — skipping)"
fi

echo "==> pre-commit: pytest -m 'not slow'"
PY=python
[ -x .venv/bin/python ] && PY=.venv/bin/python
$PY -m pytest -q --tb=short -m "not slow" || exit 1

echo "==> ✓ pre-commit checks passed"
