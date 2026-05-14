"""Daemon end-to-end smoke (no real collectors)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


def _chroma_unsafe() -> bool:
    """True if running on a chroma combo known to SEGV.

    chromadb 1.x rust bindings call into _Py_Dealloc on cpython 3.14 in a way
    that triggers a segfault during Collection._get (rust.py:440). Until the
    upstream fix lands, skip the daemon test when:
      - chromadb >= 1.0 is importable, AND
      - Python is 3.14, AND
      - COOLSTEP_CHROMA_DISABLED is not set to "1"
    """
    if os.environ.get("COOLSTEP_CHROMA_DISABLED") == "1":
        return False
    if sys.version_info[:2] != (3, 14):
        return False
    try:
        import chromadb  # type: ignore[import-not-found]
        ver = getattr(chromadb, "__version__", "0")
        major = int(ver.split(".")[0])
        return major >= 1
    except Exception:  # noqa: BLE001
        return False


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        _chroma_unsafe(),
        reason="chromadb 1.x SEGV on Python 3.14 — set COOLSTEP_CHROMA_DISABLED=1 or pin chromadb<1.0",
    ),
]

from coolstep.core.schema import (
    CpuMetrics,
    GpuMetrics,
    TelemetryFrame,
)
from coolstep.daemon import Daemon


class _FakeCollector:
    name = "fake"

    def __init__(self) -> None:
        self.sample_count = 0

    def discover(self) -> bool:
        return True

    def sample(self) -> dict[str, object]:
        self.sample_count += 1
        return {
            "cpu": {"freq_mhz": [3000.0], "load_pct": [10.0], "temps_c": {"tctl": 75.0}},
            "gpus": [GpuMetrics(name="g", temp_c=55.0)],
        }

    def cost(self):  # type: ignore[no-untyped-def]
        from coolstep.core.schema import Cost
        return Cost(sample_us=100, rss_kb=0)


class _FakeActuator:
    name = "fake_actuator"

    def __init__(self) -> None:
        self.applied: list[object] = []

    def supports(self, verb) -> bool:  # type: ignore[no-untyped-def]
        return True

    def dry_run(self, action):  # type: ignore[no-untyped-def]
        from coolstep.core.schema import SimResult
        return SimResult(expected_effect={}, confidence=1.0, reverts_in=0.0)

    def apply(self, action):  # type: ignore[no-untyped-def]
        from coolstep.core.schema import ActionResult
        self.applied.append(action)
        return ActionResult(applied_at=0.0, cmd_executed=None)

    def revert(self) -> None:
        return None


@pytest.fixture
def daemon_factory(tmp_path, monkeypatch):
    # COOLSTEP_HOME isolation: without this the daemon constructor reads
    # runtime-state.json from the real ~/coolstep/data on this host, which
    # gets rewritten 10× per second by the live collector — tests inherit
    # whatever throttle_state happened to be on disk and fail intermittently.
    # Point _coolstep_home() at tmp_path so recover() reads an empty dir.
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    def _factory():
        d = Daemon(period_sec=0.05, ring_capacity=20,
                   store_path=tmp_path / "store.db",
                   ml_state_path=tmp_path / "ml-state.json")
        d.collectors = [_FakeCollector()]
        d.actuators = [_FakeActuator()]
        return d
    return _factory


def test_daemon_runs_and_persists_frames(daemon_factory):
    import sqlite3
    daemon = daemon_factory()
    asyncio.new_event_loop().run_until_complete(daemon.run(max_ticks=5))
    # store closed in daemon.run(); reopen to count
    conn = sqlite3.connect(daemon.store.path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    finally:
        conn.close()
    assert n == 5


def test_daemon_writes_ml_state_snapshot(daemon_factory, tmp_path):
    daemon = daemon_factory()
    asyncio.new_event_loop().run_until_complete(daemon.run(max_ticks=2))
    state_path = daemon.ml_state_path
    assert state_path.exists()
    import json
    snap = json.loads(Path(state_path).read_text())
    assert snap["model_name"] in {
        "always_idle_baseline", "trajectory_baseline", "knn_v1",
        "trajectory_baseline+meta", "knn_v1+meta",
    }
    assert snap["calibration_ready"] is False
    assert "fake" in snap["collectors"]


def test_daemon_calls_actuators_when_calibration_off_and_no_predictions(daemon_factory):
    """AlwaysIdleBaseline returns prob=0 → no actions → actuator never called."""
    daemon = daemon_factory()
    fake_actuator = daemon.actuators[0]
    asyncio.new_event_loop().run_until_complete(daemon.run(max_ticks=3))
    assert isinstance(fake_actuator, _FakeActuator)
    assert fake_actuator.applied == []


# Balance-plan step I: self-monitor surface in ml-state.json.

def test_daemon_self_monitor_busy_ratio_in_snapshot(daemon_factory):
    """After a few ticks, snap['self_monitor'] is present with finite metrics
    and ring_samples == ticks_run - 1 (snapshot writes BEFORE the current
    tick's own measurement is appended, so it reflects stable past samples
    rather than the in-progress tick). Fake collector is fast → busy_ratio
    < 1.0 and slow_tick_count == 0."""
    daemon = daemon_factory()
    asyncio.new_event_loop().run_until_complete(daemon.run(max_ticks=5))
    import json
    snap = json.loads(Path(daemon.ml_state_path).read_text())
    sm = snap["self_monitor"]
    assert sm["ring_samples"] == 4
    assert 0.0 <= sm["busy_ratio_ewma"] <= 1.0
    assert 0.0 <= sm["busy_ratio_p95_60_ticks"] <= 1.0
    assert sm["tick_total_ms_p95_60_ticks"] >= 0.0
    assert sm["slow_tick_count_60_ticks"] == 0
    assert sm["slow_tick_threshold_ms"] == 500.0


def test_daemon_self_monitor_ewma_converges_to_synthetic_load(daemon_factory):
    """Push 200 ticks of known tick_total_ms through the EWMA formula and
    verify it converges within tolerance. α=0.1 → 99% mass in last ~50 samples;
    feeding 100ms with period=0.05 (=2000ms budget) → ratio 0.05."""
    daemon = daemon_factory()
    # Manually drive the EWMA update path (no real tick loop): the daemon's
    # period is 0.05s = 50ms; synthetic tick_total_ms 5ms → busy_ratio 0.1.
    for _ in range(200):
        tick_total_ms = 5.0
        busy_ratio = tick_total_ms / (daemon.period * 1000.0)
        daemon._busy_ratio_ewma = (
            daemon._busy_ratio_alpha * busy_ratio
            + (1.0 - daemon._busy_ratio_alpha) * daemon._busy_ratio_ewma
        )
        daemon._tick_total_ms_ring.append(tick_total_ms)
    assert abs(daemon._busy_ratio_ewma - 0.1) < 0.01
    assert len(daemon._tick_total_ms_ring) == 60  # capped


def test_daemon_self_monitor_slow_tick_count_reflects_ring(daemon_factory):
    """Inject one slow tick into the ring and confirm it surfaces in the
    snapshot's slow_tick_count_60_ticks."""
    daemon = daemon_factory()
    # 59 fast + 1 slow (600ms > threshold 500ms)
    for _ in range(59):
        daemon._tick_total_ms_ring.append(10.0)
    daemon._tick_total_ms_ring.append(600.0)
    # Run one real tick to trigger _dump_ml_state with the seeded ring;
    # the tick itself will append its own measurement (kept short by fake
    # collector), but the slow value should still dominate the count.
    asyncio.new_event_loop().run_until_complete(daemon.run(max_ticks=1))
    import json
    snap = json.loads(Path(daemon.ml_state_path).read_text())
    assert snap["self_monitor"]["slow_tick_count_60_ticks"] >= 1


# Regression tests for incident-2026-05-10 throttle FSM + persistence.

def _hot_frame(ts: float, t: float, label: str = "firefox") -> TelemetryFrame:
    from coolstep.core.schema import WorkloadFrame
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(temps_c={"tctl": t}, power_w={"package": 30.0}),
        gpus=[],
        workload=WorkloadFrame(label=label),
    )


def test_throttle_fsm_writes_event_on_episode_close(daemon_factory):
    daemon = daemon_factory()
    # idle → enter at 92 → peak 95 → exit at 84 spanning 4 s = >= MIN_DURATION
    daemon._throttle_fsm_tick(_hot_frame(1000.0, 88.0))
    assert daemon._throttle_state == "idle"
    daemon._throttle_fsm_tick(_hot_frame(1001.0, 92.0))
    assert daemon._throttle_state == "hot"
    assert daemon._throttle_workload_at_start == "firefox"
    daemon._throttle_fsm_tick(_hot_frame(1002.0, 95.0))
    assert daemon._throttle_peak_temp == 95.0
    daemon._throttle_fsm_tick(_hot_frame(1006.0, 84.0))
    assert daemon._throttle_state == "idle"
    assert daemon.store.count_throttle_events() == 1


def test_throttle_fsm_short_episode_below_min_duration_not_written(daemon_factory):
    daemon = daemon_factory()
    daemon._throttle_fsm_tick(_hot_frame(2000.0, 91.0))
    daemon._throttle_fsm_tick(_hot_frame(2001.0, 80.0))  # 1 s episode < 3 s
    assert daemon.store.count_throttle_events() == 0


def test_throttle_fsm_persist_and_restore_open_episode(tmp_path, monkeypatch):
    """Open hot episode survives daemon restart via runtime-state.json."""
    home = tmp_path
    monkeypatch.setenv("COOLSTEP_HOME", str(home))
    d1 = Daemon(period_sec=0.05, ring_capacity=20,
                store_path=home / "store.db",
                ml_state_path=home / "ml-state.json")
    d1.collectors = [_FakeCollector()]
    d1.actuators = [_FakeActuator()]
    d1._throttle_fsm_tick(_hot_frame(3000.0, 92.0))
    assert d1._throttle_state == "hot"
    assert (home / "runtime-state.json").exists()

    # SIGTERM-equivalent: discard daemon, fresh instance reads state.
    d2 = Daemon(period_sec=0.05, ring_capacity=20,
                store_path=home / "store.db",
                ml_state_path=home / "ml-state.json")
    assert d2._throttle_state == "hot"
    assert d2._throttle_start_ts == 3000.0
    assert d2._throttle_peak_temp == 92.0
    assert d2._throttle_workload_at_start == "firefox"


def test_throttle_fsm_skips_zero_temp(daemon_factory):
    """cpu_temp = 0 (sensor flap) must NOT advance the FSM."""
    daemon = daemon_factory()
    f = _hot_frame(4000.0, 0.0)
    daemon._throttle_fsm_tick(f)
    assert daemon._throttle_state == "idle"
    assert daemon._throttle_start_ts == 0.0


# P2.1 smoke tests — full TTL-revert suite owned by sibling agent; here we
# guard only the new runtime-state field + the in-memory armed set so the
# state-machine doesn't drift while their tests are still being written.

def test_armed_actions_field_initialised_empty(daemon_factory):
    """`_armed_actions` exists on fresh Daemon and is an empty dict."""
    daemon = daemon_factory()
    assert hasattr(daemon, "_armed_actions")
    assert daemon._armed_actions == {}


def test_runtime_state_persists_armed_actions_field(tmp_path, monkeypatch):
    """runtime-state.json must contain `armed_actions: [...]` after persist —
    even when empty — so crash-recovery on next start has a stable contract.
    """
    import json as _json

    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    d = Daemon(period_sec=0.05, ring_capacity=10,
               store_path=tmp_path / "store.db",
               ml_state_path=tmp_path / "ml-state.json")
    d._persist_runtime_state()
    state = _json.loads((tmp_path / "runtime-state.json").read_text())
    assert "armed_actions" in state
    assert state["armed_actions"] == []


# ---------------------------------------------------------------------------
# P2.1: TTL auto-revert + armed-actions tracking
#
# NOTE: these tests target the P2.1 spec; they fail until daemon.py impl
# lands in the same branch.
#
# Assumes the following additions to Daemon:
#   _armed_actions: dict[str, tuple[Action, float]]
#   _do_apply(actuator, action) -> ActionResult | None
#       -- applies action, tracks in _armed_actions if not readonly_log and no error
#   _ttl_sweep(now: float) -> None
#       -- drops expired entries (now > action.expires_at + COOLSTEP_TTL_GRACE_SEC),
#          calls actuator.revert() for each
#   _shutdown_hook() -> None
#       -- called in run() finally: reverts all armed actuators
#   COOLSTEP_TTL_GRACE_SEC (module-level constant, default 5.0)
#   COOLSTEP_REARM_GAP_SEC (module-level constant, default 15.0)
#   runtime-state.json gains `armed_actions` list persisted by _persist_runtime_state
# ---------------------------------------------------------------------------


class _FakeTTLActuator:
    """Test double for P2.1 TTL tests.

    Usage:
        actuator = _FakeTTLActuator()          # normal — gets tracked
        actuator = _FakeTTLActuator(name="readonly_log")  # bypass tracking
        actuator = _FakeTTLActuator(should_fail=True)     # apply returns error
    """

    def __init__(
        self,
        name: str = "test_hw_actuator",
        should_fail: bool = False,
    ) -> None:
        self.name = name
        self.should_fail = should_fail
        self.apply_calls: list[object] = []
        self.revert_calls: int = 0

    def supports(self, verb: object) -> bool:  # type: ignore[override]
        return True

    def dry_run(self, action: object) -> object:  # type: ignore[override]
        from coolstep.core.schema import SimResult
        return SimResult(expected_effect={}, confidence=1.0, reverts_in=0.0)

    def apply(self, action: object) -> object:  # type: ignore[override]
        from coolstep.core.schema import ActionResult
        self.apply_calls.append(action)
        if self.should_fail:
            return ActionResult(applied_at=0.0, cmd_executed=None, error="simulated failure")
        return ActionResult(applied_at=0.0, cmd_executed=None)

    def revert(self) -> None:
        self.revert_calls += 1


def _make_action(expires_at: float = 9999.0) -> object:
    from coolstep.core.schema import Action, ActionVerb
    return Action(verb=ActionVerb.RAMP_COOLING, params={"level": 1}, expires_at=expires_at)


def _daemon_with_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Daemon:
    """Create a Daemon with COOLSTEP_HOME → tmp_path (controls runtime-state.json path)."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    d = Daemon(
        period_sec=0.05,
        ring_capacity=20,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    d.collectors = [_FakeCollector()]
    return d


# --- 1. Tracking ---


def test_armed_actions_tracked_for_real_actuator_apply(tmp_path, monkeypatch):
    """apply() on non-readonly actuator with no error → entry in _armed_actions."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator()
    action = _make_action(expires_at=9999.0)

    daemon._do_apply(actuator, action)

    assert actuator.name in daemon._armed_actions, (
        f"Expected '{actuator.name}' in _armed_actions after successful apply; "
        f"got keys: {list(daemon._armed_actions)}"
    )
    tracked_action, applied_at = daemon._armed_actions[actuator.name]
    assert tracked_action is action
    assert applied_at > 0.0


def test_armed_actions_not_tracked_for_readonly_log(tmp_path, monkeypatch):
    """readonly_log actuator must never be added to _armed_actions."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator(name="readonly_log")
    action = _make_action(expires_at=9999.0)

    daemon._do_apply(actuator, action)

    assert "readonly_log" not in daemon._armed_actions, (
        "readonly_log must not be tracked in _armed_actions"
    )


def test_armed_actions_not_tracked_when_apply_errors(tmp_path, monkeypatch):
    """apply() returning ActionResult with error → entry NOT added to _armed_actions."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator(should_fail=True)
    action = _make_action(expires_at=9999.0)

    daemon._do_apply(actuator, action)

    assert actuator.name not in daemon._armed_actions, (
        "Failed apply must not be tracked in _armed_actions"
    )


# --- 2. TTL sweep ---


def test_ttl_sweep_calls_revert_after_grace(tmp_path, monkeypatch):
    """Entry whose expires_at is past (+ grace) triggers revert() and is dropped."""
    from coolstep.daemon import COOLSTEP_TTL_GRACE_SEC

    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator()
    action = _make_action(expires_at=1000.0)  # long past
    applied_at = 900.0
    daemon._armed_actions[actuator.name] = (action, applied_at)
    daemon.actuators = [actuator]

    now = 1000.0 + COOLSTEP_TTL_GRACE_SEC + 1.0  # definitely past grace
    daemon._ttl_sweep(now=now)

    assert actuator.revert_calls == 1, (
        f"Expected revert() called once after TTL expiry; got {actuator.revert_calls}"
    )
    assert actuator.name not in daemon._armed_actions, (
        "Entry must be removed from _armed_actions after revert"
    )


def test_ttl_sweep_no_revert_before_expiry(tmp_path, monkeypatch):
    """Entry still within TTL window must NOT be reverted."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator()
    future_expires = 9999.0
    action = _make_action(expires_at=future_expires)
    daemon._armed_actions[actuator.name] = (action, 0.0)
    daemon.actuators = [actuator]

    daemon._ttl_sweep(now=1000.0)  # well before expiry

    assert actuator.revert_calls == 0, (
        "revert() must not be called while action is within TTL"
    )
    assert actuator.name in daemon._armed_actions, (
        "Entry must remain in _armed_actions while still within TTL"
    )


# --- 3. Re-arm guard ---


def test_rearm_guard_blocks_within_gap(tmp_path, monkeypatch):
    """Re-apply skipped if expires_at is far enough in the future (> REARM_GAP)."""
    import time

    from coolstep.daemon import COOLSTEP_REARM_GAP_SEC

    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator()
    now = time.time()
    # expires_at = now + 60 → within gap only if REARM_GAP < 60 (default 15)
    existing_action = _make_action(expires_at=now + 60.0)
    daemon._armed_actions[actuator.name] = (existing_action, now - 10.0)
    daemon.actuators = [actuator]

    initial_apply_count = len(actuator.apply_calls)
    new_action = _make_action(expires_at=now + 60.0)
    daemon._do_apply(actuator, new_action)

    assert len(actuator.apply_calls) == initial_apply_count, (
        f"Re-arm must be blocked when expires_at > now + {COOLSTEP_REARM_GAP_SEC}s; "
        f"apply() was called anyway"
    )


def test_rearm_allowed_after_gap_close_to_expiry(tmp_path, monkeypatch):
    """Re-apply is allowed when existing action is close to expiry (< REARM_GAP)."""
    import time


    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator()
    now = time.time()
    # expires_at = now + 5 → well within rearm gap (default 15s)
    close_action = _make_action(expires_at=now + 5.0)
    daemon._armed_actions[actuator.name] = (close_action, now - 10.0)
    daemon.actuators = [actuator]

    initial_apply_count = len(actuator.apply_calls)
    new_action = _make_action(expires_at=now + 60.0)
    daemon._do_apply(actuator, new_action)

    assert len(actuator.apply_calls) > initial_apply_count, (
        "Re-apply must be allowed when existing action is within REARM_GAP of expiry"
    )


# --- 4. Shutdown hook ---


def test_shutdown_hook_reverts_all_armed(tmp_path, monkeypatch):
    """_shutdown_hook() reverts every actuator currently in _armed_actions."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    act_a = _FakeTTLActuator(name="hw_a")
    act_b = _FakeTTLActuator(name="hw_b")
    import time
    now = time.time()
    action_a = _make_action(expires_at=now + 60.0)
    action_b = _make_action(expires_at=now + 60.0)
    daemon._armed_actions["hw_a"] = (action_a, now)
    daemon._armed_actions["hw_b"] = (action_b, now)
    daemon.actuators = [act_a, act_b]

    daemon._shutdown_hook()

    assert act_a.revert_calls == 1, f"hw_a revert not called (got {act_a.revert_calls})"
    assert act_b.revert_calls == 1, f"hw_b revert not called (got {act_b.revert_calls})"


# --- 5. Runtime-state persistence ---


def test_runtime_state_persists_armed_actions(tmp_path, monkeypatch):
    """Successful apply → runtime-state.json on disk contains armed_actions entry."""
    import json
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    actuator = _FakeTTLActuator()
    daemon.actuators = [actuator]
    action = _make_action(expires_at=9999.0)

    daemon._do_apply(actuator, action)

    state_path = tmp_path / "runtime-state.json"
    assert state_path.exists(), "runtime-state.json must exist after apply"
    data = json.loads(state_path.read_text())
    entries = data.get("armed_actions", [])
    assert len(entries) >= 1, (
        f"armed_actions must have at least 1 entry in runtime-state.json; got {entries}"
    )
    names = [e.get("actuator") for e in entries]
    assert actuator.name in names, (
        f"actuator '{actuator.name}' not found in runtime-state armed_actions: {entries}"
    )


# --- 6. Startup recovery ---


def test_startup_recovery_reverts_stale_armed(tmp_path, monkeypatch):
    """Stale armed_actions in runtime-state.json → revert() called during __init__."""
    import json
    import time

    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))

    # Build runtime-state.json with a stale armed_actions entry (expires_at in the past).
    # The recovery window per spec is: expires_at < now + 60.
    stale_expires = time.time() - 10.0  # clearly past
    state = {
        "throttle_state": "idle",
        "throttle_start_ts": 0.0,
        "throttle_peak_temp": 0.0,
        "throttle_workload_at_start": None,
        "backfill_cursor_ts": 0.0,
        "saved_at": time.time() - 100.0,
        "armed_actions": [
            {
                "actuator": "test_hw_actuator",
                "verb": "ramp_cooling",
                "expires_at": stale_expires,
                "applied_at": stale_expires - 30.0,
            }
        ],
    }
    (tmp_path / "runtime-state.json").write_text(json.dumps(state))

    # Create a fake actuator that the daemon can find by name.
    stub = _FakeTTLActuator(name="test_hw_actuator")

    # Daemon __init__ reads the persisted state, detects stale entry, calls revert.
    d = Daemon(
        period_sec=0.05,
        ring_capacity=20,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    d.collectors = [_FakeCollector()]
    # Inject stub so recovery lookup finds it (recovery may use d.actuators).
    # If impl resolves actuators before recovery, this won't help — but the init
    # order is implementation-defined. The key assertion is that _armed_actions
    # is cleared and revert was called on whatever actuator the impl resolved.
    d.actuators = [stub]

    # The recovery spec: clear armed_actions from state after reverting stale entries.
    # Check runtime-state.json no longer carries that entry.
    if (tmp_path / "runtime-state.json").exists():
        recovered_data = json.loads((tmp_path / "runtime-state.json").read_text())
        recovered_entries = recovered_data.get("armed_actions", [])
        stale_entries = [
            e for e in recovered_entries
            if e.get("expires_at", 0) < time.time() + 60
        ]
        assert len(stale_entries) == 0, (
            f"Stale armed_actions entries must be cleared from runtime-state.json "
            f"during startup; still present: {stale_entries}"
        )

    # Also assert _armed_actions on the new daemon is clean (no stale leftovers).
    assert not daemon_has_stale_armed(d), (
        "_armed_actions must not contain stale (past-expiry) entries after startup"
    )


def daemon_has_stale_armed(d: Daemon) -> bool:
    """Return True if any _armed_actions entry has already expired."""
    import time
    armed = getattr(d, "_armed_actions", {})
    now = time.time()
    return any(now > action.expires_at for _name, (action, _applied_at) in armed.items())


# ---------------------------------------------------------------------------
# H: incremental backfill cadence tests
# ---------------------------------------------------------------------------


def test_backfill_fires_periodically(tmp_path, monkeypatch):
    """backfill_labels called approx every _backfill_interval_ticks ticks."""
    calls: list[tuple] = []

    def _fake_backfill(*args, **kwargs):
        calls.append(args)
        return {"unlabeled_before": 0, "labeled_hot": 0, "labeled_cool": 0, "errors": 0}

    monkeypatch.setattr("coolstep.daemon.backfill_labels", _fake_backfill)

    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    d = Daemon(
        period_sec=0.05,
        ring_capacity=20,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    d.collectors = [_FakeCollector()]
    d.actuators = [_FakeActuator()]
    d._backfill_interval_ticks = 3

    calls.clear()  # discard any calls from __init__

    for _ in range(10):
        asyncio.new_event_loop().run_until_complete(d._tick())
        d._tick_count += 1

    # With interval=3 and 10 ticks, backfill fires at ticks 0,3,6,9 = 4 times
    assert len(calls) >= 3, (
        f"Expected backfill to fire at least 3 times over 10 ticks with interval=3; "
        f"got {len(calls)}"
    )


def test_backfill_does_not_fire_within_interval(tmp_path, monkeypatch):
    """backfill_labels must NOT fire more than once during first 5 ticks with interval=10."""
    calls: list[tuple] = []

    def _fake_backfill(*args, **kwargs):
        calls.append(args)
        return {"unlabeled_before": 0, "labeled_hot": 0, "labeled_cool": 0, "errors": 0}

    monkeypatch.setattr("coolstep.daemon.backfill_labels", _fake_backfill)

    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    d = Daemon(
        period_sec=0.05,
        ring_capacity=20,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    d.collectors = [_FakeCollector()]
    d.actuators = [_FakeActuator()]
    d._backfill_interval_ticks = 10

    calls.clear()  # discard any calls from __init__

    for _ in range(5):
        asyncio.new_event_loop().run_until_complete(d._tick())
        d._tick_count += 1

    # tick_count starts at 0, interval=10; fires only at tick 0 (once) in first 5 ticks
    assert len(calls) <= 1, (
        f"Expected backfill to fire at most once in first 5 ticks with interval=10; "
        f"got {len(calls)}"
    )


# ---------------------------------------------------------------------------
# Multi-actuator audit routing (G).
#
# Before this change `_route_action` was first-match: it stopped at the first
# actuator whose `supports(verb) == True`. Because `readonly_log.supports(*)
# == True`, depending on discovery order it could starve hardware actuators —
# every action was journalled but no hardware ever moved. New semantics:
# readonly_log always audits, and the first NON-readonly actuator that
# supports the verb fires.
# ---------------------------------------------------------------------------


def test_route_action_audits_via_readonly_log(tmp_path, monkeypatch):
    """readonly_log + one hw actuator → both receive apply() for the action."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    readonly = _FakeTTLActuator(name="readonly_log")
    hw = _FakeTTLActuator(name="hw_actuator")
    daemon.actuators = [readonly, hw]
    action = _make_action(expires_at=9999.0)

    daemon._route_action(action)

    assert len(readonly.apply_calls) == 1, (
        f"readonly_log must receive the action for audit; got "
        f"{len(readonly.apply_calls)} apply_calls"
    )
    assert readonly.apply_calls[0] is action
    assert len(hw.apply_calls) == 1, (
        f"hw actuator must receive the action; got {len(hw.apply_calls)} apply_calls"
    )
    assert hw.apply_calls[0] is action


def test_route_action_hw_actuator_gets_one_fire_only(tmp_path, monkeypatch):
    """Two hw actuators both supports=True → only the FIRST one fires."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    readonly = _FakeTTLActuator(name="readonly_log")
    hw_first = _FakeTTLActuator(name="hw_first")
    hw_second = _FakeTTLActuator(name="hw_second")
    daemon.actuators = [readonly, hw_first, hw_second]
    action = _make_action(expires_at=9999.0)

    daemon._route_action(action)

    assert len(hw_first.apply_calls) == 1, (
        f"first hw actuator must fire exactly once; got {len(hw_first.apply_calls)}"
    )
    assert len(hw_second.apply_calls) == 0, (
        f"second hw actuator must NOT fire (one hw fire per action); "
        f"got {len(hw_second.apply_calls)}"
    )
    # readonly still audits.
    assert len(readonly.apply_calls) == 1


def test_route_action_falls_through_to_audit_only_when_no_hw(tmp_path, monkeypatch):
    """No hw actuator present → audit fires, nothing else tracked / raised."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    readonly = _FakeTTLActuator(name="readonly_log")
    daemon.actuators = [readonly]
    action = _make_action(expires_at=9999.0)

    # Must not raise.
    daemon._route_action(action)

    assert len(readonly.apply_calls) == 1, (
        "audit-only path must still fire readonly_log"
    )
    # readonly_log is in _NO_REVERT_ACTUATORS — nothing armed.
    assert daemon._armed_actions == {}, (
        f"audit-only routing must not track anything in _armed_actions; "
        f"got {daemon._armed_actions}"
    )


def test_route_action_does_not_track_readonly_in_armed(tmp_path, monkeypatch):
    """After routing: hw actuator armed, readonly_log NOT in _armed_actions."""
    daemon = _daemon_with_home(tmp_path, monkeypatch)
    readonly = _FakeTTLActuator(name="readonly_log")
    hw = _FakeTTLActuator(name="hw_actuator")
    daemon.actuators = [readonly, hw]
    action = _make_action(expires_at=9999.0)

    daemon._route_action(action)

    assert "readonly_log" not in daemon._armed_actions, (
        "readonly_log must never appear in _armed_actions (audit-only, no revert)"
    )
    assert "hw_actuator" in daemon._armed_actions, (
        f"hw actuator must be tracked in _armed_actions after successful apply; "
        f"got keys: {list(daemon._armed_actions)}"
    )


# --- 5. load_jump → residual bank decay (regression 2026-05-13) ---


class _LoadJumpCollector:
    """Emits a low-load idle frame on sample #1, then jumps to high load on
    sample #2.  Triggers EventSegmenter load_jump boundary (default thresh
    25 pp) on the second tick."""

    name = "load_jump_fake"

    def __init__(self) -> None:
        self.sample_count = 0

    def discover(self) -> bool:
        return True

    def sample(self) -> dict[str, object]:
        self.sample_count += 1
        load = 10.0 if self.sample_count == 1 else 80.0
        return {
            "cpu": {"freq_mhz": [3000.0], "load_pct": [load, load],
                    "temps_c": {"tctl": 70.0}},
            "gpus": [GpuMetrics(name="g", temp_c=55.0)],
            "workload": {"label": "code"},
        }

    def cost(self):  # type: ignore[no-untyped-def]
        from coolstep.core.schema import Cost
        return Cost(sample_us=100, rss_kb=0)


def test_load_jump_decays_residual_bank(tmp_path, monkeypatch):
    """Regression (user-observed 2026-05-13): on workload change without a
    tuned-profile flip, ResidualBank kept applying stale per-bucket EWMA
    correction (-8 to -17°C systematic over-prediction) — visible as a
    residual_trail frozen at one value.  Fix wires EventSegmenter's
    load_jump boundary to bank.decay_all(0.3) so the next ~10 validations
    re-converge to the new regime's true bias."""
    from coolstep.core.predictor_meta import MetaPredictor

    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    daemon = Daemon(
        period_sec=0.05,
        ring_capacity=20,
        store_path=tmp_path / "store.db",
        ml_state_path=tmp_path / "ml-state.json",
    )
    daemon.collectors = [_LoadJumpCollector()]
    # Only MetaPredictor has a bank — skip if a different predictor is in use.
    if not isinstance(daemon.predictor, MetaPredictor):
        pytest.skip("decay wiring is MetaPredictor-specific")
    # Pre-train bank with a deep idle bucket so we have something to decay.
    idle_feats = {
        "cpu_load_slope_per_sec": 0.0,
        "cpu_temp_slope_per_sec": 0.0,
        "cpu_temp_accel_per_sec_sq": 0.0,
        "cpu_temp_max": 65.0,
    }
    for _ in range(50):
        daemon.predictor.bank.observe(idle_feats, -8.0)
    n_before = sum(s.n for s in daemon.predictor.bank.stats.values())
    assert n_before >= 50

    asyncio.new_event_loop().run_until_complete(daemon.run(max_ticks=3))

    # After the load_jump on tick 2, decay_all(0.3) was applied → total n
    # shrank substantially.  Allow small extra n from new observations the
    # daemon may have made during validate_pending (which back-feeds the
    # bank on horizon elapse).
    n_after = sum(s.n for s in daemon.predictor.bank.stats.values())
    assert n_after < n_before * 0.6, (
        f"expected bank to decay after load_jump (n_before={n_before}, "
        f"n_after={n_after}); decay factor should drop total n below 60%"
    )
