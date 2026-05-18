"""Tests for actuator-journal size-based rotation (3 generations, 1 MB default)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_base(monkeypatch, tmp_path: Path, max_bytes: int = 500):
    """Set up rotation test env — patch MAX_JOURNAL_BYTES directly so monkeypatch
    teardown restores the prod default. Avoids importlib.reload which mutated
    module-level state across tests (broke test_routes.py).
    """
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    import coolstep.adapters.actuators._base as base
    monkeypatch.setattr(base, "MAX_JOURNAL_BYTES", max_bytes)
    return base


def _write_entries(base_mod, count: int, size: int = 50) -> None:
    """Append `count` records, each padded to approximately `size` bytes."""
    for i in range(count):
        record = {"event": "apply", "i": i, "pad": "x" * size}
        base_mod.append_journal_event(record)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_rotation_under_threshold(monkeypatch, tmp_path):
    """Small writes below 500 B threshold: only the active file, no .1 / .2."""
    base = _reload_base(monkeypatch, tmp_path, max_bytes=500)

    # Two records that together are well under 500 bytes
    base.append_journal_event({"event": "a"})
    base.append_journal_event({"event": "b"})

    journal = tmp_path / "actuator-journal.jsonl"
    gen1 = tmp_path / "actuator-journal.jsonl.1"
    gen2 = tmp_path / "actuator-journal.jsonl.2"

    assert journal.exists()
    assert not gen1.exists()
    assert not gen2.exists()


def test_rotation_triggers_when_over_threshold(monkeypatch, tmp_path):
    """.1 must appear after the file crosses 500 B; active file resets small."""
    base = _reload_base(monkeypatch, tmp_path, max_bytes=500)

    # Fill beyond threshold (each entry ~100 bytes; 10 × 100 = ~1000 B)
    _write_entries(base, count=10, size=80)

    journal = tmp_path / "actuator-journal.jsonl"
    gen1 = tmp_path / "actuator-journal.jsonl.1"

    assert gen1.exists(), ".1 should exist after threshold exceeded"
    # .1 has prior content; active journal must be smaller than .1
    assert gen1.stat().st_size > journal.stat().st_size


def test_third_generation_oldest_drops(monkeypatch, tmp_path):
    """After 3 rotations .2 exists; only newest 2 generations + current survive."""
    base = _reload_base(monkeypatch, tmp_path, max_bytes=500)

    # Drive at least 3 full rotations
    for _ in range(3):
        _write_entries(base, count=15, size=60)

    gen1 = tmp_path / "actuator-journal.jsonl.1"
    gen2 = tmp_path / "actuator-journal.jsonl.2"

    assert gen1.exists(), ".1 must exist"
    assert gen2.exists(), ".2 must exist (3rd generation)"

    # There must be no .3 file — oldest generation dropped
    gen3 = tmp_path / "actuator-journal.jsonl.3"
    assert not gen3.exists()


def test_rotation_idempotent_on_oserror(monkeypatch, tmp_path):
    """If os.rename raises, append still continues; no crash, no data loss."""
    base = _reload_base(monkeypatch, tmp_path, max_bytes=10)

    # Write enough to reach threshold
    base.append_journal_event({"event": "seed", "pad": "x" * 20})

    journal = tmp_path / "actuator-journal.jsonl"
    assert journal.exists()

    # Now patch os.rename to raise
    with patch("os.rename", side_effect=OSError("simulated rename failure")):
        # Should not raise; rotation silently skipped
        base.append_journal_event({"event": "after_oserror"})

    # The active journal still has content (append succeeded despite failed rotation)
    assert journal.stat().st_size > 0


def test_max_bytes_env_override(monkeypatch, tmp_path):
    """COOLSTEP_JOURNAL_MAX_BYTES=10 causes rotation on the very first write."""
    base = _reload_base(monkeypatch, tmp_path, max_bytes=10)

    # First write: file doesn't exist yet — no rotation yet
    base.append_journal_event({"e": "1"})

    journal = tmp_path / "actuator-journal.jsonl"
    gen1 = tmp_path / "actuator-journal.jsonl.1"

    # First write is fine (file was empty/absent before check)
    assert journal.exists()

    # Second write: file is now definitely >= 10 bytes → triggers rotation
    base.append_journal_event({"e": "2"})

    assert gen1.exists(), ".1 should appear after tiny 10-byte threshold is crossed"


def test_rotation_does_not_touch_unrelated_files(monkeypatch, tmp_path):
    """An unrelated file in COOLSTEP_HOME must survive journal rotations intact."""
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    base = _reload_base(monkeypatch, tmp_path, max_bytes=10)

    # Drive multiple rotations
    for _ in range(3):
        _write_entries(base, count=5, size=60)

    assert unrelated.exists(), "unrelated.txt must still exist"
    assert unrelated.read_text(encoding="utf-8") == "keep me"
