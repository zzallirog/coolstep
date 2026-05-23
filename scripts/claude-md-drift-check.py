#!/usr/bin/env python3
"""Detect drift between CLAUDE.md docs and the code/files they describe.

Checks performed per child CLAUDE.md:

  1. `Last synced with master:` line — flagged if older than the most-recent
     commit touching this subdir.
  2. `Active collectors` / `Active actuators` / `Active units` tables — row
     count is compared against the actual number of non-helper files in the
     directory described.
  3. `Routes` table (dashboard child) — counts entries vs `@app.{get,post,...}`
     decorators in `coolstep/dashboard/server.py` + `routes/*.py`.
  4. Master CLAUDE.md `Repo version:` vs `pyproject.toml [project] version`.

Exit codes:
  0 — no drift
  1 — drift found (and --fix not requested)

Modes:
  default (report)  — print findings, do not edit
  --bump-stamps     — write today's date into `Last synced with master:` for
                       every child whose subdir has commits newer than the
                       stamp. Tables stay untouched (content drift needs
                       manual editing — the report still shows it).
  --json            — emit findings as JSON for tooling/hooks

Designed to be cheap (no imports of the package, no daemon). Suitable for
a pre-push git hook: report-only, blocks push if drift is "structural"
(table mismatch), not just stamp-only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── ground-truth probes ──────────────────────────────────────────────────


def _count_py_modules(d: Path) -> int:
    """Count adapter/collector modules in a directory: *.py minus _base.py and __init__.py."""
    if not d.is_dir():
        return 0
    return sum(
        1 for p in d.glob("*.py")
        if not p.name.startswith("_")
    )


def _route_paths(root: Path) -> set[str]:
    """Extract literal route path strings from `@app.{verb}("/api/...")` decorators."""
    paths: list[Path] = []
    server = root / "coolstep" / "dashboard" / "server.py"
    if server.exists():
        paths.append(server)
    routes_dir = root / "coolstep" / "dashboard" / "routes"
    if routes_dir.is_dir():
        paths.extend(routes_dir.glob("*.py"))
    pat = re.compile(r'@app\.(?:get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']')
    out: set[str] = set()
    for p in paths:
        try:
            out.update(pat.findall(p.read_text()))
        except OSError:
            continue
    return out


def _count_systemd_units(root: Path) -> int:
    """Count .service / .timer files under systemd/."""
    d = root / "systemd"
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.suffix in {".service", ".timer"})


def _count_adr_headers(root: Path) -> int:
    f = root / "docs" / "stack-decisions.md"
    if not f.exists():
        return 0
    return sum(1 for line in f.read_text().splitlines() if line.startswith("## ADR-"))


def _pyproject_version(root: Path) -> str | None:
    pp = root / "pyproject.toml"
    if not pp.exists():
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', pp.read_text(), re.M)
    return m.group(1) if m else None


def _subdir_for(claude_md: Path) -> Path:
    """The directory whose state this CLAUDE.md describes."""
    return claude_md.parent


def _last_commit_date(subdir: Path) -> str | None:
    """Most recent git commit touching anything in subdir. Returns YYYY-MM-DD or None."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(REPO), "log", "-1", "--format=%cs", "--", str(subdir)],
            capture_output=True, timeout=4, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = cp.stdout.decode().strip()
    return out or None


# ── per-child checks ─────────────────────────────────────────────────────


@dataclass
class Finding:
    file: str
    kind: str   # "stamp" | "structure" | "version"
    message: str


@dataclass
class FileReport:
    path: Path
    stamp: str | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_structural(self) -> bool:
        return any(f.kind == "structure" for f in self.findings)

    @property
    def has_any(self) -> bool:
        return bool(self.findings)


STAMP_RE = re.compile(r"^\*\*Last synced with master:\*\*\s*(\S+)\s*$", re.M)
# Match table rows that start with `| `backtick`some_file.py`backtick — adapter listings.
ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)\.py`", re.M)


def _scan_child(path: Path) -> FileReport:
    text = path.read_text()
    rep = FileReport(path=path)

    m = STAMP_RE.search(text)
    if m:
        rep.stamp = m.group(1)
    else:
        rep.findings.append(Finding(str(path), "stamp",
                                    "missing `Last synced with master:` line"))

    subdir = _subdir_for(path)
    last_commit = _last_commit_date(subdir)
    if rep.stamp and last_commit and last_commit > rep.stamp:
        rep.findings.append(Finding(
            str(path), "stamp",
            f"stamp {rep.stamp} older than newest commit in subdir {last_commit}",
        ))

    # Structure checks per known child.
    rel = str(path.relative_to(REPO))
    if rel == "coolstep/adapters/collectors/CLAUDE.md":
        actual = _count_py_modules(subdir)
        rows = len(ROW_RE.findall(text))
        if rows and rows != actual:
            rep.findings.append(Finding(
                rel, "structure",
                f"claims {rows} collector files in table, disk has {actual}",
            ))
    elif rel == "coolstep/adapters/actuators/CLAUDE.md":
        actual = _count_py_modules(subdir)
        rows = len(ROW_RE.findall(text))
        if rows and rows != actual:
            rep.findings.append(Finding(
                rel, "structure",
                f"claims {rows} actuator files in table, disk has {actual}",
            ))
    elif rel == "coolstep/dashboard/CLAUDE.md":
        # Membership check: every actual route path must appear in the doc
        # text (anywhere — listed in the routes table or referenced in prose
        # both count). Strip path params {…} for a fair textual match.
        actual = _route_paths(REPO)
        missing: list[str] = []
        for path in sorted(actual):
            needle = re.sub(r"\{[^}]+\}", "{", path)  # /api/x/{ts}/y → /api/x/{
            stem = needle.split("{")[0].rstrip("/")
            if stem and stem not in text:
                missing.append(path)
        if missing:
            preview = ", ".join(missing[:5]) + (f" (+{len(missing)-5} more)" if len(missing) > 5 else "")
            rep.findings.append(Finding(
                rel, "structure",
                f"{len(missing)} route path(s) in code not mentioned in doc: {preview}",
            ))
    elif rel == "systemd/CLAUDE.md":
        actual = _count_systemd_units(REPO)
        rows = len(re.findall(r"^\|\s*`[\w.-]+\.(service|timer)`", text, re.M))
        if rows and rows != actual:
            rep.findings.append(Finding(
                rel, "structure",
                f"claims {rows} unit files in table, disk has {actual}",
            ))
    elif rel == "docs/CLAUDE.md":
        # docs/CLAUDE.md doesn't carry an ADR count, but if anything cites
        # "N ADRs" we cross-check against the actual count.
        m = re.search(r"(\d+)\s+ADRs", text)
        if m:
            claimed = int(m.group(1))
            actual = _count_adr_headers(REPO)
            if actual and claimed != actual:
                rep.findings.append(Finding(
                    rel, "structure",
                    f"claims {claimed} ADRs but stack-decisions.md has {actual}",
                ))

    return rep


def _scan_master(path: Path) -> FileReport:
    text = path.read_text()
    rep = FileReport(path=path)
    m = re.search(r"\*\*Repo version:\*\*\s*([^\s(]+)", text)
    if m:
        claimed = m.group(1).lstrip("v")
        actual = _pyproject_version(REPO)
        if actual and claimed != actual:
            rep.findings.append(Finding(
                str(path), "version",
                f"Repo version claim '{claimed}' diverges from pyproject '{actual}'",
            ))
    return rep


# ── stamp autofix ────────────────────────────────────────────────────────


def _bump_stamp(path: Path) -> bool:
    text = path.read_text()
    today = date.today().isoformat()
    new = STAMP_RE.sub(f"**Last synced with master:** {today}", text)
    if new != text:
        path.write_text(new)
        return True
    return False


# ── main ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bump-stamps", action="store_true",
                    help="rewrite Last-synced date to today for stale children")
    ap.add_argument("--json", action="store_true",
                    help="emit findings as JSON")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-file 'ok' lines")
    args = ap.parse_args(argv)

    claude_files = sorted(
        p for p in REPO.rglob("CLAUDE.md")
        if ".venv" not in p.parts
        and ".claude" not in p.parts
        and "build" not in p.parts
        and "dist" not in p.parts
        and "__pycache__" not in p.parts
    )

    if not claude_files:
        print("no CLAUDE.md files found", file=sys.stderr)
        return 1

    reports: list[FileReport] = []
    master = REPO / "CLAUDE.md"
    for f in claude_files:
        if f == master:
            reports.append(_scan_master(f))
        else:
            reports.append(_scan_child(f))

    # Stamp autofix pass — only after the report is built so JSON output
    # still shows what changed.
    bumped: list[str] = []
    if args.bump_stamps:
        for rep in reports:
            if rep.path == master:
                continue
            if any(f.kind == "stamp" for f in rep.findings):
                if _bump_stamp(rep.path):
                    bumped.append(str(rep.path.relative_to(REPO)))

    if args.json:
        out = {
            "reports": [
                {
                    "file": str(r.path.relative_to(REPO)),
                    "stamp": r.stamp,
                    "findings": [
                        {"kind": f.kind, "message": f.message} for f in r.findings
                    ],
                }
                for r in reports
            ],
            "bumped": bumped,
        }
        print(json.dumps(out, indent=2))
    else:
        any_finding = False
        for rep in reports:
            rel = rep.path.relative_to(REPO)
            if not rep.findings:
                if not args.quiet:
                    print(f"  ✓ {rel}  (stamp={rep.stamp or '—'})")
                continue
            any_finding = True
            for f in rep.findings:
                glyph = "⚠" if f.kind == "stamp" else "✗"
                print(f"  {glyph} {rel}  [{f.kind}] {f.message}")
        if bumped:
            print(f"\nbumped {len(bumped)} stamp(s) → today:")
            for b in bumped:
                print(f"    {b}")
        if not any_finding and not bumped:
            print("\nno drift detected")

    # Exit code: drift only counts if NOT just stamps OR if user didn't
    # ask --bump-stamps. Structural/version drift always blocks.
    blocking = any(
        f.kind in {"structure", "version"}
        for rep in reports for f in rep.findings
    )
    stamp_only = any(
        f.kind == "stamp" for rep in reports for f in rep.findings
    ) and not blocking

    if blocking:
        return 1
    if stamp_only and not args.bump_stamps:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
