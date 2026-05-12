"""Readonly actuator tests."""

from __future__ import annotations

from coolstep.adapters.actuators.readonly import ReadonlyActuator
from coolstep.core.schema import Action, ActionVerb


def _action(verb: ActionVerb = ActionVerb.NOTIFY_USER, expires: float = 1000.0) -> Action:
    return Action(verb=verb, params={"x": 1}, expires_at=expires)


def test_supports_every_verb():
    a = ReadonlyActuator()
    for verb in ActionVerb:
        assert a.supports(verb)


def test_apply_appends_to_journal():
    a = ReadonlyActuator()
    assert list(a.journal()) == []
    a.apply(_action(ActionVerb.RAMP_COOLING))
    a.apply(_action(ActionVerb.CAP_BOOST))
    journal = list(a.journal())
    assert len(journal) == 2
    assert "ramp_cooling" in journal[0].stdout_tail
    assert "cap_boost" in journal[1].stdout_tail


def test_dry_run_returns_simresult():
    a = ReadonlyActuator()
    sim = a.dry_run(_action())
    assert sim.expected_effect["would_apply"] == 1.0


def test_revert_is_noop():
    a = ReadonlyActuator()
    a.apply(_action())
    assert a.revert() is None  # smoke


def test_journal_capacity_caps():
    a = ReadonlyActuator(journal_capacity=3)
    for _ in range(10):
        a.apply(_action())
    assert len(list(a.journal())) == 3


def test_make_returns_actuator():
    from coolstep.adapters.actuators import readonly

    instance = readonly.make()
    assert isinstance(instance, ReadonlyActuator)


def test_actuator_registry_finds_readonly():
    from coolstep.adapters.actuators import discover

    found = discover()
    assert any(a.name == "readonly_log" for a in found)
