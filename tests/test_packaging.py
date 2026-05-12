"""Packaging guard tests — G-1, G-3, G-6.

These run on every test-suite execution, so PKGBUILD/wheel drift is caught
before AUR push or `pip install` from a release tag.

See `docs/aur-publishing.md` for case studies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKGBUILDS = [
    _REPO_ROOT / "packaging" / "aur" / "coolstep" / "PKGBUILD",
    _REPO_ROOT / "packaging" / "aur" / "coolstep-git" / "PKGBUILD",
]


def _existing_pkgbuilds() -> list[Path]:
    return [p for p in _PKGBUILDS if p.exists()]


# ---------------------------------------------------------------- G-3 guard

def test_pkgbuilds_install_all_systemd_units() -> None:
    """Every coolstep-*.service/.timer in systemd/ must be installed by every PKGBUILD."""
    systemd_dir = _REPO_ROOT / "systemd"
    repo_units = sorted(
        list(systemd_dir.glob("coolstep-*.service"))
        + list(systemd_dir.glob("coolstep-*.timer"))
    )
    assert repo_units, f"no coolstep-* units found in {systemd_dir} — wrong path?"

    pkgbuilds = _existing_pkgbuilds()
    assert pkgbuilds, "no PKGBUILD files found"

    failures: list[str] = []
    for pkgbuild_path in pkgbuilds:
        content = pkgbuild_path.read_text()
        for unit in repo_units:
            if unit.name not in content:
                failures.append(f"{pkgbuild_path.name} doesn't install {unit.name}")
    assert not failures, (
        "PKGBUILD missing systemd units (G-3 regression):\n  "
        + "\n  ".join(failures)
    )


# ---------------------------------------------------------------- G-6 guard

def test_dashboard_static_files_packaged() -> None:
    """static/ assets must live next to coolstep.dashboard.__init__."""
    import coolstep.dashboard

    pkg_root = Path(coolstep.dashboard.__file__).parent
    static = pkg_root / "static"
    assert static.exists(), (
        f"static/ missing from package at {static} — G-6 regression. "
        "Check [tool.setuptools.package-data] in pyproject.toml."
    )
    index = static / "index.html"
    assert index.exists(), f"index.html missing at {index}"
    assert index.stat().st_size > 0, "index.html empty"

    js_files = list(static.rglob("*.js"))
    assert len(js_files) >= 10, (
        f"only {len(js_files)} JS files in static/ — Lit components missing?"
    )


def test_dashboard_root_returns_200_html() -> None:
    """G-6 functional smoke — GET / must serve HTML, not 500.

    Uses `create_app()` factory per dashboard/CLAUDE.md invariant
    ("не использовать singleton app — иначе тесты делят state").
    """
    from fastapi.testclient import TestClient

    from coolstep.dashboard.server import create_app

    response = TestClient(create_app()).get("/")
    assert response.status_code == 200, (
        f"GET / returned {response.status_code} (G-6 regression — static/index.html "
        f"not found?).  Body: {response.text[:200]}"
    )
    assert b"<html" in response.content.lower()
    assert b"coolstep" in response.content.lower()


def test_pkgbuild_install_lists_static_belt_check() -> None:
    """Belt-and-suspenders: PKGBUILD doesn't need explicit static/ lines
    (setuptools package-data handles it), but if a future maintainer regresses
    pyproject, this test catches the wheel-side gap.  Run only if `build`
    module available."""
    try:
        import build  # noqa: F401
    except ImportError:
        pytest.skip("python-build not installed")

    # Sanity — pyproject lists dashboard package-data
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    assert "coolstep.dashboard" in pyproject, (
        "pyproject [tool.setuptools.package-data] missing 'coolstep.dashboard' "
        "entry — G-6 will regress when wheel is rebuilt"
    )
    assert "static/" in pyproject, "package-data should list static/* patterns"


# ---------------------------------------------------------------- G-1 guard (soft)

def test_pkgbuild_checkdepends_lists_httpx() -> None:
    """G-1 — fastapi.testclient needs httpx.  PKGBUILD must list it in
    checkdepends (or makedepends as fallback)."""
    failures: list[str] = []
    for pkgbuild_path in _existing_pkgbuilds():
        content = pkgbuild_path.read_text()
        if "python-httpx" not in content:
            failures.append(f"{pkgbuild_path.name} missing python-httpx")
    assert not failures, (
        "PKGBUILD missing python-httpx in checkdepends (G-1 regression):\n  "
        + "\n  ".join(failures)
    )
