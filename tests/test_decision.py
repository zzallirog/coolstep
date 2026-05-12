"""Decision engine tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coolstep.core.decision import DecisionEngine, Thresholds, _ramp_intensity_for
from coolstep.core.predictor import Prediction
from coolstep.core.schema import ActionVerb

# Default labeled_count for tests that pre-date P2.2 (KNN auto-fire gate).
# Choose a value well above the default min_arm_labeled_count=5 so the soft
# bias path stays open in pre-P2.2 scenarios.
_WARM_KNN = 50


@pytest.fixture(autouse=True)
def _clear_p22_env(monkeypatch):
    """P2.2 env hatches + decisions log must not leak between tests.

    `COOLSTEP_FORCE_FIRE_RAMP` and `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION`
    are read on every `decide()` call — a stray export in the parent shell
    would silently flip the bias path open in unrelated tests.

    `COOLSTEP_DECISIONS_LOG_DISABLED=1` keeps the test suite from writing to
    the user's real `~/coolstep/data/decisions.jsonl`. The logging-specific
    tests below clear this knob locally and pin `COOLSTEP_HOME` to tmp_path.
    """
    monkeypatch.delenv("COOLSTEP_FORCE_FIRE_RAMP", raising=False)
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION", raising=False)
    monkeypatch.setenv("COOLSTEP_DECISIONS_LOG_DISABLED", "1")


def _pred(prob: float, conf: float = 0.9) -> Prediction:
    return Prediction(
        horizon_sec=5.0,
        throttle_prob=prob,
        expected_temp_c=85.0,
        confidence=conf,
        model_name="test",
        features_used=[],
    )


def test_low_confidence_emits_nothing():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95, conf=0.3), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    assert actions == []


def test_below_notify_threshold_emits_nothing():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.2), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    assert actions == []


def test_notify_only_at_low_threshold():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.5), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    assert [a.verb for a in actions] == [ActionVerb.NOTIFY_USER]


def test_soft_actions_at_mid_threshold():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.75), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    verbs = sorted(a.verb.value for a in actions)
    assert verbs == ["cap_boost", "notify_user", "ramp_cooling"]


def test_hard_actions_at_high_threshold():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    verbs = sorted(a.verb.value for a in actions)
    assert "shift_power_envelope" in verbs
    assert "ramp_cooling" in verbs
    assert "cap_boost" in verbs
    assert "notify_user" in verbs


def test_calibration_not_ready_blocks_hardware_actions():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=False, now=100.0,
                            labeled_count=_WARM_KNN)
    verbs = [a.verb for a in actions]
    # Только notify допускается без калибровки
    assert verbs == [ActionVerb.NOTIFY_USER]


def test_custom_thresholds_respected():
    engine = DecisionEngine(thresholds=Thresholds(notify_prob=0.9, soft_action_prob=0.95,
                                                  hard_action_prob=0.99, min_confidence=0.0))
    actions = engine.decide(_pred(0.5), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    assert actions == []  # prob 0.5 ниже notify_prob 0.9


def test_action_expires_at_set():
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    for a in actions:
        assert a.expires_at > 100.0


# --- P2.2 KNN auto-fire gates ---------------------------------------------


def test_p22_backcompat_two_arg_call_still_works():
    """Old `decide(prediction, calibration_ready)` shape must remain valid.

    Behaviour shifts: the KNN bias path now requires `labeled_count >= 5`,
    so the 2-arg call (which falls back to labeled_count=0) yields no
    RAMP_COOLING — but the call itself must not error.
    """
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), True)  # noqa: FBT003 — 2-arg legacy shape
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING not in verbs
    # Other verbs still fire — confirms the call landed and the rest of
    # the pipeline ran on legacy args.
    assert ActionVerb.SHIFT_POWER_ENVELOPE in verbs
    assert ActionVerb.NOTIFY_USER in verbs


def test_p22_labeled_count_below_threshold_blocks_ramp_cooling():
    """`labeled_count < min_arm_labeled_count` → no RAMP_COOLING fired."""
    engine = DecisionEngine()  # default min_arm_labeled_count=5
    actions = engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                            labeled_count=2)  # below threshold
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING not in verbs
    # Other verbs unaffected by the KNN coverage gate.
    assert ActionVerb.SHIFT_POWER_ENVELOPE in verbs
    assert ActionVerb.CAP_BOOST in verbs


def test_p22_labeled_count_at_threshold_allows_ramp_cooling():
    """`labeled_count == min_arm_labeled_count` → RAMP_COOLING fires."""
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                            labeled_count=5)
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING in verbs


def test_p22_relaxed_confidence_floor_for_ramp_cooling():
    """Confidence between min_arm_confidence and min_confidence allows
    RAMP_COOLING but NOT the harder verbs."""
    engine = DecisionEngine()  # min_arm_confidence=0.5, min_confidence=0.6
    actions = engine.decide(_pred(0.95, conf=0.55), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING in verbs
    assert ActionVerb.SHIFT_POWER_ENVELOPE not in verbs
    assert ActionVerb.CAP_BOOST not in verbs
    assert ActionVerb.NOTIFY_USER not in verbs


def test_p22_force_fire_env_bypasses_all_gates(monkeypatch):
    """COOLSTEP_FORCE_FIRE_RAMP=1 → RAMP_COOLING fires even with cold KNN,
    low confidence, AND calibration not ready."""
    monkeypatch.setenv("COOLSTEP_FORCE_FIRE_RAMP", "1")
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.1, conf=0.05),       # below every threshold
        calibration_ready=False,      # blocked normally
        now=100.0,
        labeled_count=0,              # cold KNN
    )
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING in verbs
    # Force-fire is RAMP_COOLING-only; other verbs stay locked.
    assert ActionVerb.SHIFT_POWER_ENVELOPE not in verbs
    assert ActionVerb.CAP_BOOST not in verbs


@pytest.mark.parametrize("falsy", ["0", "false", "False", "no", "off", ""])
def test_p22_force_fire_falsy_values_do_not_bypass(monkeypatch, falsy):
    """Truthiness parsing: falsy strings must NOT trigger the bypass."""
    monkeypatch.setenv("COOLSTEP_FORCE_FIRE_RAMP", falsy)
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                            labeled_count=0)  # cold KNN
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING not in verbs


def test_p22_allow_pre_calibration_env_releases_ramp_only(monkeypatch):
    """COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1 → RAMP_COOLING fires while
    calibration_ready=False, but the harder verbs still stay blocked."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION", "1")
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=False, now=100.0,
                            labeled_count=_WARM_KNN)
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING in verbs
    assert ActionVerb.SHIFT_POWER_ENVELOPE not in verbs
    assert ActionVerb.CAP_BOOST not in verbs


def test_p22_rearm_gap_field_default_mirrors_daemon():
    """`Thresholds.rearm_gap_s` must mirror `daemon.REARM_GAP_SEC` default
    (15.0s). This is an informational field — daemon owns enforcement."""
    from coolstep.daemon import REARM_GAP_SEC
    assert Thresholds().rearm_gap_s == REARM_GAP_SEC


def test_p22_new_threshold_defaults_unchanged_for_legacy_fields():
    """P2.2 may NOT change existing Thresholds defaults — only ADD fields."""
    t = Thresholds()
    assert t.notify_prob == 0.4
    assert t.soft_action_prob == 0.7
    assert t.hard_action_prob == 0.9
    assert t.min_confidence == 0.6
    # New fields, documented defaults.
    assert t.min_arm_confidence == 0.5
    assert t.min_arm_labeled_count == 5
    assert t.rearm_gap_s == 15.0


# --- intensity_pct scaling (Task A) ------------------------------------------


def test_intensity_floor_at_low_prob():
    """prob=0.65 → (0.65-0.5)*40 = 6.0 — just above the 5.0 floor."""
    assert _ramp_intensity_for(0.65) == pytest.approx(6.0)


def test_intensity_mid_range():
    """prob=0.85 → (0.85-0.5)*40 = 14.0."""
    assert _ramp_intensity_for(0.85) == pytest.approx(14.0)


def test_intensity_ceiling_at_full_prob():
    """prob=1.0 → (1.0-0.5)*40 = 20.0 (ceiling)."""
    assert _ramp_intensity_for(1.0) == pytest.approx(20.0)


def test_intensity_clamped_at_floor_for_sub_threshold_prob():
    """Any prob that would produce < 5 is clamped to 5.0 (defence in depth)."""
    assert _ramp_intensity_for(0.2) == pytest.approx(5.0)


def test_intensity_pct_in_ramp_action_scales_with_prob():
    """RAMP_COOLING action carries intensity derived from the formula, not a
    hardcoded value."""
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.85), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)
    ramp = next(a for a in actions if a.verb == ActionVerb.RAMP_COOLING)
    assert ramp.params["intensity_pct"] == pytest.approx(14.0)


# --- decisions.jsonl append-only log -----------------------------------------


def _read_log_lines(home: Path) -> list[dict]:
    """Parse `data/decisions.jsonl` under tmp_path-style COOLSTEP_HOME."""
    path = home / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_decide_appends_to_decisions_log(tmp_path, monkeypatch):
    """One decide() call → one JSON line with the spec's schema fields."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_DECISIONS_LOG_DISABLED", raising=False)
    engine = DecisionEngine()
    actions = engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                            labeled_count=_WARM_KNN)

    lines = _read_log_lines(tmp_path)
    assert len(lines) == 1
    rec = lines[0]
    # Spec schema sanity — top-level keys.
    assert set(rec.keys()) == {
        "ts", "prediction", "calibration_ready", "labeled_count",
        "actions", "thresholds",
    }
    # Prediction sub-object.
    assert rec["prediction"]["throttle_prob"] == pytest.approx(0.95)
    assert rec["prediction"]["model_name"] == "test"
    # Calibration + count round-trip.
    assert rec["calibration_ready"] is True
    assert rec["labeled_count"] == _WARM_KNN
    # Thresholds match Thresholds() defaults.
    assert rec["thresholds"]["soft_action_prob"] == pytest.approx(0.7)
    assert rec["thresholds"]["min_arm_labeled_count"] == 5
    # Actions logged match what decide() returned (by verb).
    logged_verbs = sorted(a["verb"] for a in rec["actions"])
    returned_verbs = sorted(a.verb.value for a in actions)
    assert logged_verbs == returned_verbs


def test_decide_log_appends_on_each_call(tmp_path, monkeypatch):
    """Three decide() calls → three lines, append-only (no overwrite)."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_DECISIONS_LOG_DISABLED", raising=False)
    engine = DecisionEngine()
    # Mix: hot, idle, mid — cover both "actions=[]" and populated lists.
    engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                  labeled_count=_WARM_KNN)
    engine.decide(_pred(0.05), calibration_ready=True, now=101.0,
                  labeled_count=_WARM_KNN)
    engine.decide(_pred(0.75), calibration_ready=True, now=102.0,
                  labeled_count=_WARM_KNN)

    lines = _read_log_lines(tmp_path)
    assert len(lines) == 3
    # The idle call recorded an empty actions list — exactly the case the
    # actuator-journal can't capture and that motivates this log.
    assert lines[1]["actions"] == []
    # Hot + mid recorded non-empty lists.
    assert lines[0]["actions"]
    assert lines[2]["actions"]


def test_decide_log_disabled_via_env(tmp_path, monkeypatch):
    """COOLSTEP_DECISIONS_LOG_DISABLED=1 → no file created."""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.setenv("COOLSTEP_DECISIONS_LOG_DISABLED", "1")
    engine = DecisionEngine()
    engine.decide(_pred(0.95), calibration_ready=True, now=100.0,
                  labeled_count=_WARM_KNN)

    assert not (tmp_path / "decisions.jsonl").exists()


# ── P2.3 — quiet-mode REDUCE_NOISE ─────────────────────────────────────


def test_quiet_mode_calm_emits_reduce_noise():
    """Confidently-calm prediction + cool chip + warm KNN → REDUCE_NOISE fires."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.05, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="quiet", cpu_temp_c=60.0,
    )
    verbs = [a.verb for a in actions]
    assert ActionVerb.REDUCE_NOISE in verbs
    quiet_action = next(a for a in actions if a.verb == ActionVerb.REDUCE_NOISE)
    # Action carries the eject ceiling for the actuator-side safety belt.
    assert "eject_temp_c" in quiet_action.params
    assert quiet_action.params["eject_temp_c"] > 0


def test_quiet_mode_hot_chip_blocks_reduce_noise():
    """Predictor calm but Tctl above the quiet ceiling → no REDUCE_NOISE."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.05, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="quiet", cpu_temp_c=78.0,
    )
    assert ActionVerb.REDUCE_NOISE not in [a.verb for a in actions]


def test_quiet_mode_uncertain_predictor_blocks_reduce_noise():
    """Low predictor confidence → quiet-mode bias refuses to fire."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.05, conf=0.3), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="quiet", cpu_temp_c=60.0,
    )
    # Confidence below min_arm floor short-circuits decide() to []
    # (existing behaviour). Either way: no REDUCE_NOISE.
    assert ActionVerb.REDUCE_NOISE not in [a.verb for a in actions]


def test_quiet_mode_unknown_temp_blocks_reduce_noise():
    """`cpu_temp_c=None` means «I don't know» → refuse to bias."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.05, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="quiet", cpu_temp_c=None,
    )
    assert ActionVerb.REDUCE_NOISE not in [a.verb for a in actions]


def test_quiet_mode_prediction_above_calm_ceiling_blocks():
    """throttle_prob > quiet_calm_prob_max → no REDUCE_NOISE."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.25, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="quiet", cpu_temp_c=60.0,
    )
    assert ActionVerb.REDUCE_NOISE not in [a.verb for a in actions]


def test_cool_mode_never_emits_reduce_noise():
    """Default `mode="cool"` keeps the original policy — no REDUCE_NOISE."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.05, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="cool", cpu_temp_c=60.0,
    )
    assert ActionVerb.REDUCE_NOISE not in [a.verb for a in actions]


def test_off_mode_blocks_all_actionable_verbs_keeps_notify():
    """`mode="off"` blocks RAMP/CAP/SHIFT but lets NOTIFY_USER flow."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.95, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="off", cpu_temp_c=85.0,
    )
    verbs = {a.verb for a in actions}
    assert verbs == {ActionVerb.NOTIFY_USER}


def test_quiet_mode_safety_path_still_allows_ramp_cooling():
    """In quiet mode, if the chip actually starts heating up the predictor
    will produce a high-prob frame — RAMP_COOLING must still be allowed so
    quiet bias is overridden by the safety path on the *next* tick."""
    engine = DecisionEngine()
    actions = engine.decide(
        _pred(0.85, conf=0.9), calibration_ready=True, now=100.0,
        labeled_count=_WARM_KNN, mode="quiet", cpu_temp_c=72.0,
    )
    verbs = {a.verb for a in actions}
    assert ActionVerb.RAMP_COOLING in verbs
    assert ActionVerb.REDUCE_NOISE not in verbs


def test_quiet_threshold_defaults_match_spec():
    """Defensive: lock the quiet-mode threshold defaults so a silent change
    surfaces as a test failure rather than a behaviour drift."""
    t = Thresholds()
    assert t.quiet_calm_prob_max == 0.15
    assert t.quiet_calm_temp_max_c == 75.0
    assert t.quiet_eject_temp_c == 80.0
    assert t.quiet_negative_intensity_pct == 10.0
    assert t.quiet_min_confidence == 0.6
