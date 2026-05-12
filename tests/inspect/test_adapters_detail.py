"""Tests for coolstep/inspect/adapters_detail.py."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coolstep.core.schema import ActionVerb
from coolstep.inspect.adapters_detail import (
    detail_all,
    detail_for,
)


# ---------------------------------------------------------------------------
# Fake actuator helpers
# ---------------------------------------------------------------------------


class _FakeActuator:
    """Minimal actuator stub for tests — supports RAMP_COOLING only."""

    def __init__(self, name: str, supported_verbs: list[ActionVerb] | None = None) -> None:
        self.name = name
        self._verbs: set[ActionVerb] = set(supported_verbs or [ActionVerb.RAMP_COOLING])

    def supports(self, verb: ActionVerb) -> bool:
        return verb in self._verbs

    def dry_run(self, action):  # type: ignore[override]
        raise NotImplementedError

    def apply(self, action):  # type: ignore[override]
        raise NotImplementedError

    def revert(self) -> None:
        pass


def _set_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1 — empty COOLSTEP_HOME
# ---------------------------------------------------------------------------


def test_detail_for_actuator_no_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path)
    act = _FakeActuator("test_act")
    d = detail_for(act)

    assert d["armed"]["armed"] is False
    assert d["journal_count_last_1000"] == 0
    # non-asusctl actuator → baseline dict is empty
    assert d["baseline"] == {}


# ---------------------------------------------------------------------------
# Test 2 — asusctl actuator with baseline file
# ---------------------------------------------------------------------------


def test_detail_for_asusctl_with_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path)
    baseline = {
        "profile": "Performance",
        "fan": "cpu",
        "anchors": [{"temp": i * 10, "pwm": 50 + i} for i in range(8)],
    }
    (tmp_path / "asusctl_fan_curve_baseline.json").write_text(json.dumps(baseline))

    act = _FakeActuator("asusctl_fan_curve_bias")
    d = detail_for(act)

    assert d["baseline"]["baseline_present"] is True
    assert d["baseline"]["baseline_anchor_count"] == 8
    assert d["baseline"]["baseline_profile"] == "Performance"
    # age should be very small (written moments ago)
    assert d["baseline"]["baseline_age_sec"] < 5.0


# ---------------------------------------------------------------------------
# Test 3 — armed entry in runtime-state.json
# ---------------------------------------------------------------------------


def test_detail_armed_status_from_runtime_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path)
    expires = time.time() + 10
    state = {
        "armed_actions": [
            {"actuator": "test_act", "verb": "ramp_cooling", "expires_at": expires}
        ]
    }
    (tmp_path / "runtime-state.json").write_text(json.dumps(state))

    act = _FakeActuator("test_act")
    d = detail_for(act)

    assert d["armed"]["armed"] is True
    assert d["armed"]["verb"] == "ramp_cooling"
    assert d["armed"]["ttl_remaining_sec"] > 5.0


# ---------------------------------------------------------------------------
# Test 4 — journal counting
# ---------------------------------------------------------------------------


def test_detail_journal_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path)
    journal_path = tmp_path / "actuator-journal.jsonl"
    lines = []
    for _ in range(3):
        lines.append(json.dumps({"actuator": "test_act", "kind": "apply"}))
    for _ in range(2):
        lines.append(json.dumps({"actuator": "other_act", "kind": "apply"}))
    journal_path.write_text("\n".join(lines) + "\n")

    act = _FakeActuator("test_act")
    d = detail_for(act)

    assert d["journal_count_last_1000"] == 3


# ---------------------------------------------------------------------------
# Test 5 — expired armed entry clamps ttl to 0
# ---------------------------------------------------------------------------


def test_detail_armed_ttl_remaining_clamps_at_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path)
    past_expires = time.time() - 60  # expired 1 minute ago
    state = {
        "armed_actions": [
            {"actuator": "test_act", "verb": "cap_boost", "expires_at": past_expires}
        ]
    }
    (tmp_path / "runtime-state.json").write_text(json.dumps(state))

    act = _FakeActuator("test_act")
    d = detail_for(act)

    assert d["armed"]["armed"] is True
    assert d["armed"]["ttl_remaining_sec"] == 0.0


# ---------------------------------------------------------------------------
# Test 6 — detail_all returns one dict per actuator
# ---------------------------------------------------------------------------


def test_detail_all_returns_one_dict_per_actuator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_home(monkeypatch, tmp_path)
    acts = [_FakeActuator("act_alpha"), _FakeActuator("act_beta")]
    result = detail_all(acts)

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert names == {"act_alpha", "act_beta"}
    for r in result:
        assert "supports" in r
        assert "armed" in r
        assert "baseline" in r
        assert "journal_count_last_1000" in r
