"""G-4 + G-5 guards — `coolstep install-units` behavior.

G-4: on headless (no XDG_RUNTIME_DIR), suggest `loginctl enable-linger`
     instead of bare `systemctl --user enable` (which would fail).
G-5: must create `~/coolstep/data/` so units don't loop on `status=226/NAMESPACE`.

See `docs/aur-publishing.md` § G-4 and § G-5 for case studies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


# ---------------------------------------------------------------- G-5 guard

def test_install_units_creates_data_dir(isolated_home: Path) -> None:
    from coolstep.inspect.install_units import install, _data_dir

    assert not _data_dir().exists(), "precondition: data dir absent"
    results = install(force=True)

    assert _data_dir().exists(), (
        f"install() must create {_data_dir()} (G-5 regression — units would "
        f"die with status=226/NAMESPACE on first start)"
    )
    assert _data_dir().is_dir()
    statuses = {target: status for target, status in results}
    assert str(_data_dir()) in statuses, "data dir must be in returned results"


def test_install_units_idempotent_on_data_dir(isolated_home: Path) -> None:
    from coolstep.inspect.install_units import install, _data_dir

    _data_dir().mkdir(parents=True)
    results = install(force=True)
    statuses = {target: status for target, status in results}
    assert "skipped" in statuses[str(_data_dir())].lower()


# ---------------------------------------------------------------- G-4 guard

def test_install_units_headless_branch(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When XDG_RUNTIME_DIR unset (headless SSH), CLI must emit linger instructions."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    from coolstep.inspect.cli import main

    result = CliRunner().invoke(main, ["install-units", "--force"])
    assert result.exit_code == 0, f"command failed: {result.output}"
    out = result.output
    assert "loginctl enable-linger" in out, (
        "headless branch must instruct user to enable linger "
        f"(G-4 regression). Output:\n{out}"
    )
    assert "XDG_RUNTIME_DIR" in out, "must instruct on XDG_RUNTIME_DIR export"


def test_install_units_session_branch(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When XDG_RUNTIME_DIR set + exists, CLI gives normal systemctl --user steps."""
    runtime = tmp_path / "run-user"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    from coolstep.inspect.cli import main

    result = CliRunner().invoke(main, ["install-units", "--force"])
    assert result.exit_code == 0
    out = result.output
    assert "systemctl --user daemon-reload" in out
    assert "systemctl --user enable --now" in out
    assert "loginctl enable-linger" not in out, (
        "session branch should NOT show headless headlines"
    )
