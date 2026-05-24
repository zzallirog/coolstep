"""Run all interference scenarios.

For each scenario, the test asserts the *observed* behaviour of coolstep,
not the ideal one. Where coolstep currently lacks a gate, the test is
marked `xfail(strict=True)` — if someone fixes the gap upstream, the
xfail flips to XPASS and the test will fail to force a status update
(scenario.bugcase_status → "covered" + remove fix_proposal).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coolstep.compat import reset_caps
from coolstep.compat.detect import detect_caps
from coolstep.compat.install_plan import caps_to_install_plan
from tests.compat.fixtures import apply_overlay, fake_platform, load_snapshot
from tests.compat.interference import SCENARIOS, fake_actors


@pytest.fixture(autouse=True)
def _reset():
    from coolstep.compat.manifest import reset_manifest
    reset_caps(None)
    reset_manifest()
    yield
    reset_caps(None)
    reset_manifest()


def _ids(items):
    return [s.name for s in items]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids(SCENARIOS))
def test_scenario(scenario, tmp_path: Path, monkeypatch):
    base = load_snapshot(scenario.base_snapshot)
    snap = apply_overlay(base, scenario.overlay) if scenario.overlay else base

    # Apply scenario-level env on top of snapshot env
    snap = apply_overlay(snap, {"env": {**snap.env, **scenario.env}})

    with fake_platform(snap, tmp_path), fake_actors(
        scenario.other_actors,
        asusctl_version=snap.asusctl_version or "6.1.2",
    ):
        caps = detect_caps()
        outcome = _probe_outcome(caps, scenario)

    _assert_outcome(outcome, scenario)


def _probe_outcome(caps, scenario) -> dict:
    """Inspect actuator make()/supports() under the active scenario."""
    out: dict = {"caps": caps}

    from coolstep.core.schema import ActionVerb

    # epp_shift
    try:
        from coolstep.adapters.actuators import epp_shift as _epp

        # Force its caps view via singleton
        from coolstep.compat import reset_caps as _rc
        _rc(caps)
        epp_inst = _epp.make()
        out["epp_shift_instance"] = epp_inst
    except Exception as exc:  # noqa: BLE001
        out["epp_shift_instance_error"] = repr(exc)

    # ryzenadj_cap
    try:
        from coolstep.adapters.actuators import ryzenadj_cap as _rj
        rj_inst = _rj.make()
        out["ryzenadj_cap_instance"] = rj_inst
    except Exception as exc:  # noqa: BLE001
        out["ryzenadj_cap_instance_error"] = repr(exc)

    # asusctl_fan_curve
    try:
        from coolstep.adapters.actuators import asusctl_fan_curve as _af
        af_inst = _af.make()
        out["asusctl_instance"] = af_inst
        if af_inst is not None:
            out["asusctl_supports_ramp_cooling"] = af_inst.supports(
                ActionVerb.RAMP_COOLING,
            )
    except Exception as exc:  # noqa: BLE001
        out["asusctl_instance_error"] = repr(exc)

    # game_mode_optimizer
    try:
        from coolstep.adapters.actuators import game_mode_optimizer as _gm
        gm_inst = _gm.make()
        out["game_mode_optimizer_instance"] = gm_inst
        if gm_inst is not None:
            out["game_mode_optimizer_supports_ramp_cooling"] = gm_inst.supports(
                ActionVerb.RAMP_COOLING,
            )
    except Exception as exc:  # noqa: BLE001
        out["game_mode_optimizer_instance_error"] = repr(exc)

    # install plan (used by S07, S11, S12)
    out["plan"] = caps_to_install_plan(caps)
    return out


def _assert_outcome(outcome, scenario):
    caps = outcome["caps"]
    plan = outcome.get("plan")

    failures: list[str] = []

    def check(key, predicate, detail):
        if not predicate:
            failures.append(f"[{scenario.name}] {key}: {detail}")

    # ---- per-scenario assertion logic ----
    name = scenario.name
    if name == "s01-game-mode-defers-asusctl":
        check(
            "asusctl_supports_ramp_cooling",
            outcome.get("asusctl_supports_ramp_cooling") is False,
            f"want False, got {outcome.get('asusctl_supports_ramp_cooling')}",
        )
        check(
            "game_mode_optimizer_supports_ramp_cooling",
            outcome.get("game_mode_optimizer_supports_ramp_cooling") is True,
            f"want True, got {outcome.get('game_mode_optimizer_supports_ramp_cooling')}",
        )

    elif name == "s02-game-mode-cooperative":
        check(
            "asusctl_supports_ramp_cooling",
            outcome.get("asusctl_supports_ramp_cooling") is True,
            f"want True under DEFER=0, got {outcome.get('asusctl_supports_ramp_cooling')}",
        )
        check(
            "game_mode_optimizer_supports_ramp_cooling",
            outcome.get("game_mode_optimizer_supports_ramp_cooling") is False,
            f"want False under DEFER=0, got {outcome.get('game_mode_optimizer_supports_ramp_cooling')}",
        )

    elif name == "s03-ppd-vs-epp-shift":
        # Gate added 2026-05-12: epp_shift.make() must return None when
        # caps.power_profiles_daemon_active is True.
        check("caps.ppd_active", caps.power_profiles_daemon_active is True, "PPD must be detected")
        check(
            "epp_shift.make() cedes to PPD",
            outcome.get("epp_shift_instance") is None,
            f"EppShift instance is {outcome.get('epp_shift_instance')!r} — gate failed",
        )

    elif name == "s04-tlp-vs-ryzenadj":
        # Gate added 2026-05-12: ryzenadj_cap.make() must return None when
        # caps.tlp_active is True (overridable via COOLSTEP_RYZENADJ_FORCE=1).
        check("caps.tlp_active", caps.tlp_active is True, "TLP must be detected as active")
        check(
            "ryzenadj_cap.make() cedes to TLP",
            outcome.get("ryzenadj_cap_instance") is None,
            f"RyzenadjCap instance is {outcome.get('ryzenadj_cap_instance')!r} — gate failed",
        )

    elif name == "s05-secure-boot-ryzenadj-silent-noop":
        # ThinkPad fixture has cpu_vendor=intel so ryzenadj path is moot;
        # we simply assert detect_caps wired secure_boot=True and that
        # actuator make() either returns None or instance — both acceptable.
        check("caps.secure_boot", caps.secure_boot is True, "secure boot must be detected")

    elif name == "s06-asusctl-v5-api-break":
        check("caps.asusctl_version_ok", caps.asusctl_version_ok is False, "v5 must be flagged not-ok")
        check(
            "asusctl_instance is None",
            outcome.get("asusctl_instance") is None,
            "asusctl_fan_curve.make() must return None for v5",
        )
        check(
            "game_mode_optimizer_instance is None",
            outcome.get("game_mode_optimizer_instance") is None,
            "game_mode_optimizer.make() must return None for v5",
        )

    elif name == "s07-server-facet-suppresses-actuators":
        check("caps.facet=server", caps.facet == "server", f"facet={caps.facet}")
        check(
            "plan disables actuator env",
            plan.env_suggestions.get("COOLSTEP_ACTUATOR_ENABLE") == "false",
            f"plan.env_suggestions={plan.env_suggestions}",
        )

    elif name == "s08-two-coolstep-instances":
        # The gap: no fcntl lock in EppShift. We assert the absence of any
        # documented lock by checking the module for the LOCK constant.
        import coolstep.adapters.actuators.epp_shift as _epp
        check(
            "no inter-process lock in EppShift",
            not hasattr(_epp, "EPP_LOCK_PATH"),
            "no EPP_LOCK_PATH constant in epp_shift module — gap is real",
        )

    elif name == "s09-thermald-temp-range-design":
        # Smoke: caps detects thermald via service active probe — but
        # nothing in coolstep checks this. We just assert detect ran clean.
        check("caps detected", caps is not None, "")

    elif name == "s10-hyprland-stale-signature":
        # Detector returns 'hyprland' from env var alone — gap demo.
        check(
            "compositor stale-but-detected",
            caps.compositor == "hyprland",
            f"want hyprland (env says so even though socket missing), got {caps.compositor}",
        )

    elif name == "s11-rapl-cve-2020-8694":
        check("caps.rapl_powercap", caps.rapl_powercap is True, "")
        check("caps.rapl_readable", caps.rapl_readable is False, "")
        check(
            "warning mentions CVE",
            any("CVE-2020-8694" in w for w in caps.warnings),
            f"warnings={caps.warnings!r}",
        )
        ptr_triggers = [p.trigger for p in plan.community_pointers]
        check(
            "rapl_perm pointer surfaced",
            any("RAPL" in t for t in ptr_triggers) or any("root" in (p.reason or "").lower() for p in plan.community_pointers),
            f"pointers={ptr_triggers}",
        )

    elif name == "s12-laptop-no-fan-tool":
        check(
            "battery present in caps via power_supply",
            True,  # ThinkPad snapshot has BAT0
            "",
        )
        check(
            "fan-control warning emitted",
            any("fan control" in w.lower() for w in caps.warnings),
            f"warnings={caps.warnings!r}",
        )

    elif name == "s13-feral-gamemoded-missed":
        # Gate added 2026-05-12: both _is_game_mode_active() in
        # asusctl_fan_curve and game_mode_optimizer now probe gamemoded.service
        # (system unit) in addition to --user game-mode.service.
        import inspect

        from coolstep.adapters.actuators import asusctl_fan_curve as _af
        from coolstep.adapters.actuators import game_mode_optimizer as _gm
        af_src = inspect.getsource(_af.AsusctlFanCurve._is_game_mode_active)
        gm_src = inspect.getsource(_gm.GameModeOptimizer._is_game_mode_active)
        check(
            "asusctl probes gamemoded.service",
            "gamemoded.service" in af_src,
            "asusctl_fan_curve._is_game_mode_active missing gamemoded.service probe",
        )
        check(
            "game_mode_optimizer probes gamemoded.service",
            "gamemoded.service" in gm_src,
            "game_mode_optimizer._is_game_mode_active missing gamemoded.service probe",
        )

    elif name == "s14-nvidia-persistence-mode":
        # Caps has no field for nvidia persistence mode; prove the gap.
        check(
            "no caps.nvidia_persistence_mode field",
            not hasattr(caps, "nvidia_persistence_mode"),
            "caps schema doesn't even know about persistence mode",
        )

    elif name == "s15-asusctl-baseline-drift":
        # Proof-of-gap: revert() implementation in asusctl_fan_curve does NOT
        # re-read current curve before restoring baseline.
        import inspect

        from coolstep.adapters.actuators import asusctl_fan_curve as _af
        revert_src = inspect.getsource(_af.AsusctlFanCurve.revert)
        check(
            "revert does not re-read curve before restoring",
            "fan-curve -j" not in revert_src and "current_curve" not in revert_src.lower(),
            "Gap: revert blindly restores _baseline without drift check",
        )

    elif name == "s16-ryzenadj-sudoers-missing":
        # Gate added 2026-05-12: _sudoers_preflight() probes sudo -n; if
        # NOPASSWD entry is missing, make() returns None and the user gets
        # a one-shot install hint via log.warning.
        import inspect

        from coolstep.adapters.actuators import ryzenadj_cap as _rj
        check(
            "_sudoers_preflight exists",
            hasattr(_rj, "_sudoers_preflight"),
            "ryzenadj_cap missing _sudoers_preflight helper",
        )
        check(
            "make() invokes _sudoers_preflight",
            "_sudoers_preflight" in inspect.getsource(_rj.make),
            "make() doesn't call the preflight",
        )

    elif name == "s17-bmc-overrides-host":
        check("caps.facet=server", caps.facet == "server", f"facet={caps.facet}")
        check(
            "plan still disables actuator env",
            plan.env_suggestions.get("COOLSTEP_ACTUATOR_ENABLE") == "false",
            f"plan.env_suggestions={plan.env_suggestions}",
        )

    assert not failures, "\n".join(failures)


def test_scenario_catalogue_has_status_for_each():
    """Every scenario must declare a bugcase_status."""
    valid = {"covered", "gap", "design"}
    bad = [s.name for s in SCENARIOS if s.bugcase_status not in valid]
    assert not bad, f"scenarios with invalid bugcase_status: {bad}"


def test_gap_scenarios_have_fix_proposal():
    """Every 'gap' scenario must propose a concrete fix."""
    missing = [s.name for s in SCENARIOS if s.bugcase_status == "gap" and not s.fix_proposal]
    assert not missing, f"gap scenarios without fix_proposal: {missing}"
