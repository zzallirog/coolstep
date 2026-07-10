"""Adapter-test fixtures.

Scoped to `tests/adapters/` because the batch-defer sentinel probe only
affects actuator `supports()` resolution, which lives here.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_batch_defer_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the batch-defer sentinel probe at a fresh, empty tmp dir.

    `is_batch_defer_active()` (actuators/_base.py) reads a sentinel file under
    COOLSTEP_HOME. Without isolation a real `batch-defer.flag` on the dev box
    (left by the off-hours reading-batch-power run) would make actuator
    `supports()` return False mid-test and flake the game-mode truth-table
    tests. Each adapter test gets its own COOLSTEP_HOME with no flag, so the
    probe is deterministically inactive unless the test explicitly creates the
    flag. Tests that need their own COOLSTEP_HOME just set the env var again —
    monkeypatch applies last-write-wins within the test body.
    """
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path / "coolstep-home"))
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER", raising=False)
    monkeypatch.delenv("COOLSTEP_BATCH_DEFER_PATH", raising=False)
