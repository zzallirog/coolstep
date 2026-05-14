"""G-2 guard — `__version__` must stay synced with pyproject.toml.

Background: pre-0.5.0 release had `coolstep/__init__.py:__version__ = "0.0.1"`
while `pyproject.toml` was already at `0.5.0`.  `pacman -Qi` reported 0.5.0,
`python -c 'from coolstep import __version__'` reported 0.0.1 — drift.

See `docs/aur-publishing.md` § G-2 for full case study.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import coolstep

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version_synced_with_pyproject() -> None:
    pyproject = _REPO_ROOT / "pyproject.toml"
    assert pyproject.exists(), f"missing {pyproject}"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    pyproject_version = data["project"]["version"]
    assert coolstep.__version__ == pyproject_version, (
        f"version drift — coolstep/__init__.py says {coolstep.__version__!r}, "
        f"pyproject.toml says {pyproject_version!r}. "
        f"Bump both together (see docs/aur-publishing.md § G-2)."
    )


def test_version_synced_with_aur_pkgbuild() -> None:
    """Soft check — only against stable PKGBUILD (not -git, which uses dynamic pkgver)."""
    pkgbuild = _REPO_ROOT / "packaging" / "aur" / "coolstep" / "PKGBUILD"
    if not pkgbuild.exists():
        return  # PKGBUILD not present in checkout
    for line in pkgbuild.read_text().splitlines():
        if line.startswith("pkgver="):
            pkgver = line.split("=", 1)[1].strip().strip("'\"")
            assert pkgver == coolstep.__version__, (
                f"AUR stable PKGBUILD pkgver={pkgver!r} doesn't match "
                f"coolstep.__version__={coolstep.__version__!r}"
            )
            return
    raise AssertionError("pkgver= line not found in stable PKGBUILD")
