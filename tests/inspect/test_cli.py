"""G-13 guard — `coolstep tail` accepts POSIX `-n N` shortcut for `--ticks`."""

from __future__ import annotations

from click.testing import CliRunner

from coolstep.inspect.cli import main, tail


def test_tail_help_advertises_n_alias() -> None:
    """Both `-n` and `--ticks` must surface in --help so operators discover it."""
    result = CliRunner().invoke(tail, ["--help"])
    assert result.exit_code == 0
    assert "-n" in result.output
    assert "--ticks" in result.output


def test_tail_n_short_flag_is_recognised() -> None:
    """Pre-fix behaviour was `Error: No such option: -n` (exit 2)."""
    result = CliRunner().invoke(main, ["tail", "-n", "0"])
    assert "No such option: -n" not in result.output
