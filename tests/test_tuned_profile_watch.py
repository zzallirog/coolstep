"""Tuned profile watcher — file reads, missing-file fallback, change detection."""

from __future__ import annotations

from pathlib import Path

from coolstep.core.tuned_profile_watch import ProfileWatcher, current_profile


def test_current_profile_reads_one_line(tmp_path: Path):
    p = tmp_path / "active_profile"
    p.write_text("workstation-latency-quiet\n")
    assert current_profile(p) == "workstation-latency-quiet"


def test_current_profile_strips_whitespace_and_newlines(tmp_path: Path):
    p = tmp_path / "active_profile"
    p.write_text("  balanced  \n\n")
    assert current_profile(p) == "balanced"


def test_current_profile_missing_returns_none(tmp_path: Path):
    p = tmp_path / "does_not_exist"
    assert current_profile(p) is None


def test_current_profile_empty_file_returns_none(tmp_path: Path):
    p = tmp_path / "active_profile"
    p.write_text("\n")
    assert current_profile(p) is None


def test_profile_watcher_first_poll_returns_current(tmp_path: Path):
    p = tmp_path / "active_profile"
    p.write_text("balanced\n")
    w = ProfileWatcher(path=p)
    assert w.poll(now=100.0) == "balanced"
    assert w.last_seen == "balanced"
    assert w.last_changed_ts == 100.0


def test_profile_watcher_unchanged_returns_none(tmp_path: Path):
    p = tmp_path / "active_profile"
    p.write_text("balanced\n")
    w = ProfileWatcher(path=p)
    w.poll(now=100.0)
    assert w.poll(now=110.0) is None
    assert w.last_changed_ts == 100.0  # unchanged


def test_profile_watcher_detects_change(tmp_path: Path):
    p = tmp_path / "active_profile"
    p.write_text("workstation-max\n")
    w = ProfileWatcher(path=p)
    assert w.poll(now=100.0) == "workstation-max"
    p.write_text("workstation-latency-quiet\n")
    assert w.poll(now=200.0) == "workstation-latency-quiet"
    assert w.last_changed_ts == 200.0


def test_profile_watcher_file_appears_mid_run(tmp_path: Path):
    """File starts missing, then appears.  Both transitions are reported."""
    p = tmp_path / "active_profile"
    w = ProfileWatcher(path=p)
    assert w.poll(now=100.0) is None  # first poll: missing → seen=None
    p.write_text("balanced\n")
    assert w.poll(now=150.0) == "balanced"


def test_profile_watcher_file_becomes_missing(tmp_path: Path):
    """File disappears (tuned removed?) — transition to None reported."""
    p = tmp_path / "active_profile"
    p.write_text("balanced\n")
    w = ProfileWatcher(path=p)
    w.poll(now=100.0)
    p.unlink()
    # The transition to None is observable: poll returns None but last_seen
    # changes to None.  poll() returning None is ambiguous (unchanged OR
    # transition-to-None), so consumers should check last_seen explicitly.
    w.poll(now=200.0)
    assert w.last_seen is None
    assert w.last_changed_ts == 200.0


def test_profile_watcher_three_transitions(tmp_path: Path):
    """Sequence: max → latency-quiet → balanced → latency-quiet."""
    p = tmp_path / "active_profile"
    p.write_text("workstation-max\n")
    w = ProfileWatcher(path=p)
    assert w.poll(now=10.0) == "workstation-max"
    p.write_text("workstation-latency-quiet\n")
    assert w.poll(now=20.0) == "workstation-latency-quiet"
    p.write_text("balanced\n")
    assert w.poll(now=30.0) == "balanced"
    p.write_text("workstation-latency-quiet\n")
    assert w.poll(now=40.0) == "workstation-latency-quiet"
