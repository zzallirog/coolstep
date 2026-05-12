#!/usr/bin/env bash
# coolstep pre-commit checks — ruff + pytest fast subset.
# Run manually: scripts/pre-commit.sh
# Installed by scripts/install-hooks.sh as .git/hooks/pre-commit (symlink).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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
