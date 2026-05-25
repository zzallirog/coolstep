"""G-12 guard — every entry point exposes `--version`.

Before the fix, all four scripts (`coolstep`, `coolstep-collector`,
`coolstep-dashboard`, `coolstep-mcp`) failed with `Error: No such option:
--version`. Operator had to fall back on `pip show coolstep` to learn the
installed version — non-obvious on hosts they didn't provision themselves.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from coolstep import __version__


def _assert_prints_version(result, prog: str) -> None:
    assert result.exit_code == 0, result.output
    assert __version__ in result.output
    assert prog in result.output


def test_inspect_cli_version() -> None:
    from coolstep.inspect.cli import main

    result = CliRunner().invoke(main, ["--version"])
    _assert_prints_version(result, "coolstep")


def test_collector_version() -> None:
    daemon = pytest.importorskip("coolstep.daemon")

    result = CliRunner().invoke(daemon.run_collector, ["--version"])
    _assert_prints_version(result, "coolstep-collector")


def test_dashboard_version() -> None:
    server = pytest.importorskip("coolstep.dashboard.server")

    result = CliRunner().invoke(server.run_dashboard, ["--version"])
    _assert_prints_version(result, "coolstep-dashboard")


def test_mcp_version(monkeypatch, capsys) -> None:
    mcp = pytest.importorskip("coolstep.mcp_server")

    monkeypatch.setattr("sys.argv", ["coolstep-mcp", "--version"])
    mcp.main()
    captured = capsys.readouterr()
    assert __version__ in captured.out
    assert "coolstep-mcp" in captured.out
