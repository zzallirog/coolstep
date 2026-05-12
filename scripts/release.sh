#!/usr/bin/env bash
# scripts/release.sh — version bump + CHANGELOG promotion + git tag.
#
# Usage:
#   scripts/release.sh v0.5.0
#   scripts/release.sh v0.5.0 --dry-run    # show actions without executing
#   scripts/release.sh v0.5.0 --no-push    # tag locally, don't push
#
# Required state:
#   - clean working tree (no staged/unstaged changes)
#   - on master branch (or override with --branch)
#   - CHANGELOG.md has [Unreleased] section with non-empty content

set -euo pipefail

VERSION=""
DRY_RUN=false
DO_PUSH=true
BRANCH="master"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --no-push) DO_PUSH=false ;;
        --branch) shift; BRANCH="$1" ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^#//' | head -20
            exit 0 ;;
        v[0-9]*)
            VERSION="$1" ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

if [ -z "$VERSION" ]; then
    echo "ERROR: missing version. usage: $0 vX.Y.Z" >&2
    exit 1
fi

# Validate semver
if ! [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
    echo "ERROR: '$VERSION' is not vMAJOR.MINOR.PATCH[-pre]" >&2
    exit 1
fi

VERSION_NO_V="${VERSION#v}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Check branch
current_branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$current_branch" != "$BRANCH" ]; then
    echo "ERROR: on '$current_branch', need '$BRANCH'. Use --branch to override." >&2
    exit 1
fi

# Check clean tree
if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree dirty. Commit/stash first." >&2
    git status --short
    exit 1
fi

# Check tag doesn't exist yet
if git rev-parse "$VERSION" >/dev/null 2>&1; then
    echo "ERROR: tag '$VERSION' already exists" >&2
    exit 1
fi

# CHANGELOG.md must have [Unreleased] with content
if ! [ -f CHANGELOG.md ]; then
    echo "ERROR: CHANGELOG.md missing" >&2
    exit 1
fi

unreleased_lines=$(awk '/^## \[Unreleased\]/,/^## \[/' CHANGELOG.md | tail -n +2 | head -n -1 | grep -v "^$" | wc -l)
if [ "$unreleased_lines" -lt 2 ]; then
    echo "WARNING: [Unreleased] section seems empty (only $unreleased_lines content lines)" >&2
    if [ "$DRY_RUN" = false ]; then
        read -rp "Continue anyway? [y/N] " yn
        [ "$yn" = "y" ] || [ "$yn" = "Y" ] || exit 1
    fi
fi

TODAY=$(date +%Y-%m-%d)

echo "==> coolstep release"
echo "    version: $VERSION"
echo "    today:   $TODAY"
echo "    push:    $DO_PUSH"
echo "    dry-run: $DRY_RUN"
echo ""

_run() {
    if [ "$DRY_RUN" = true ]; then
        echo "  DRY-RUN: $*"
    else
        echo "  + $*"
        "$@"
    fi
}

# Promote CHANGELOG.md [Unreleased] → [vX.Y.Z]
# In-place edit via python to be safe with markdown.
echo "--- (1/4) promote CHANGELOG.md [Unreleased] → [$VERSION] $TODAY ---"
if [ "$DRY_RUN" = false ]; then
    python3 - <<PY
import re
from pathlib import Path

p = Path("CHANGELOG.md")
text = p.read_text()

# Replace the first [Unreleased] heading with a fresh placeholder above
# the versioned section; content (the new release notes) stays in place.
new_section_header = "## [Unreleased]\n\nNothing pending — all work merged to master.\n\n---\n\n## [$VERSION] — $TODAY"

pattern = re.compile(r"^## \[Unreleased\]\n", re.MULTILINE)
m = pattern.search(text)
if not m:
    raise SystemExit("CHANGELOG.md missing [Unreleased] heading")

new_text = pattern.sub(new_section_header + "\n", text, count=1)

# Append tag-link footer (best-effort, only if not already present).
if "[$VERSION]:" not in new_text:
    footer = f"\n[$VERSION]: https://gitea.strong-host.net/zzalli/coolstep/releases/tag/$VERSION\n"
    new_text += footer

p.write_text(new_text)
print(f"  CHANGELOG.md updated: [Unreleased] -> [$VERSION]")
PY
else
    echo "  DRY-RUN: would edit CHANGELOG.md"
fi

# Bump __version__ if exists
echo "--- (2/4) bump __version__ ---"
INIT="coolstep/__init__.py"
if [ -f "$INIT" ] && grep -q "^__version__" "$INIT"; then
    if [ "$DRY_RUN" = false ]; then
        sed -i.bak "s/^__version__.*\$/__version__ = \"$VERSION_NO_V\"/" "$INIT"
        rm -f "${INIT}.bak"
        echo "  $INIT updated to $VERSION_NO_V"
    else
        echo "  DRY-RUN: would update $INIT to $VERSION_NO_V"
    fi
else
    echo "  ($INIT has no __version__ — skipping)"
fi

# Commit
echo "--- (3/4) commit ---"
if [ "$DRY_RUN" = false ]; then
    git add CHANGELOG.md "$INIT" 2>/dev/null || true
    git commit -m "release: $VERSION"
    echo "  + git add + git commit"
else
    echo "  DRY-RUN: git add CHANGELOG.md $INIT"
    echo "  DRY-RUN: git commit -m \"release: $VERSION\""
fi

# Tag
echo "--- (4/4) tag $VERSION ---"
_run git tag -a "$VERSION" -m "$VERSION — $TODAY"

if [ "$DO_PUSH" = true ] && [ "$DRY_RUN" = false ]; then
    echo ""
    read -rp "Push to remotes? [y/N] " yn
    if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
        for remote in $(git remote); do
            echo "  + git push $remote $BRANCH"
            git push "$remote" "$BRANCH" || echo "    (push to $remote failed — continuing)"
            echo "  + git push $remote $VERSION"
            git push "$remote" "$VERSION" || echo "    (tag push to $remote failed — continuing)"
        done
    else
        echo "  (skipped — push manually with: git push <remote> $BRANCH && git push <remote> $VERSION)"
    fi
fi

echo ""
echo "==> release $VERSION ready"
