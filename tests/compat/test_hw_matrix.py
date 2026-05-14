"""Hardware matrix integration test.

Parametrise over every snapshot in tests/compat/fixtures/<name>/snapshot.py
and run detect_caps() → caps_to_install_plan() end-to-end against a
materialised fakeroot tree.

Each snapshot's `expected` dict declares the fields the test must verify.
This replaces a 12-VM lab — same coverage, runs in <1s, lands in CI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coolstep.compat.detect import detect_caps
from coolstep.compat.install_plan import caps_to_install_plan
from tests.compat.fixtures import fake_platform, list_snapshots, load_snapshot


@pytest.fixture(autouse=True)
def _reset_singletons():
    from coolstep.compat import reset_caps
    from coolstep.compat.manifest import reset_manifest
    reset_caps(None)
    reset_manifest()
    yield
    reset_caps(None)
    reset_manifest()


# ---------------------------------------------------------------------------
# detect_caps smoke — runs once per snapshot, asserts expected fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("snapshot_name", list_snapshots())
def test_detect_caps_smoke(snapshot_name: str, tmp_path: Path):
    """detect_caps() returns a PlatformCaps with snapshot.expected fields."""
    snap = load_snapshot(snapshot_name)
    with fake_platform(snap, tmp_path):
        caps = detect_caps()

    failures: list[str] = []
    for key, want in snap.expected.items():
        # Some "expected" keys are install-plan assertions, skip here
        if key.startswith("expected_"):
            continue
        if not hasattr(caps, key):
            # Treat as derived property check via getattr
            try:
                got = getattr(caps, key)
            except AttributeError:
                failures.append(f"{snapshot_name}: caps has no field/property `{key}`")
                continue
        else:
            got = getattr(caps, key)
        if got != want:
            failures.append(f"{snapshot_name}.{key}: expected {want!r}, got {got!r}")

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# install_plan integration — runs caps_to_install_plan and verifies
# expected_actuators / community pointers / aur packages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("snapshot_name", list_snapshots())
def test_install_plan_for_snapshot(snapshot_name: str, tmp_path: Path):
    snap = load_snapshot(snapshot_name)
    with fake_platform(snap, tmp_path):
        caps = detect_caps()
        plan = caps_to_install_plan(caps)

    # Plan should always be serialisable
    j = plan.to_json()
    assert isinstance(j, str) and len(j) > 50

    # Distro fields propagate
    assert plan.distro_id == caps.distro_id
    assert plan.distro_clan == caps.distro_clan
    assert plan.pkg_manager == caps.pkg_manager

    # Active actuators (excluding readonly_log) match expected when provided
    expected_acts: list[str] | None = snap.expected.get("expected_actuators")
    if expected_acts is not None:
        active = [a.name for a in plan.actuators if a.will_activate and a.name != "readonly_log"]
        # readonly_log appears via CollectorStatus, filter by name
        active = [n for n in active if n != "readonly_log"]
        assert sorted(active) == sorted(expected_acts), (
            f"{snapshot_name}: actuators differ\n"
            f"  expected: {sorted(expected_acts)}\n"
            f"  got:      {sorted(active)}"
        )

    # Expected community pointers — by id
    expected_ptr_ids: list[str] | None = snap.expected.get("expected_community_pointer_ids")
    if expected_ptr_ids is not None:
        # We need to map pointer trigger text back to id — use manifest
        from coolstep.compat.install_plan import _load_pointers_by_id
        id_to_ptr = _load_pointers_by_id()
        # Match by reference equality on (trigger, reason) — pointers in plan
        # come from the same manifest, so trigger text is stable.
        trigger_to_id = {p.trigger: pid for pid, p in id_to_ptr.items()}
        got_ids = [trigger_to_id.get(p.trigger, "?") for p in plan.community_pointers]
        for needed in expected_ptr_ids:
            assert needed in got_ids, (
                f"{snapshot_name}: expected community pointer `{needed}` missing.\n"
                f"  got: {got_ids}"
            )


# ---------------------------------------------------------------------------
# Loader sanity — each fixture provides minimum DSL fields
# ---------------------------------------------------------------------------

def test_loader_discovers_all_snapshots():
    names = list_snapshots()
    assert len(names) >= 5, f"need at least 5 snapshots, got {names}"
    for n in names:
        snap = load_snapshot(n)
        assert snap.name, f"{n}: missing `name`"
        assert snap.description, f"{n}: missing `description`"
        assert snap.os_release, f"{n}: missing `os_release`"
        assert snap.expected, f"{n}: missing `expected` assertions"
