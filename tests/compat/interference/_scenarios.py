"""Interference scenario DSL + global subprocess/os patch layer.

Each scenario is a `Scenario` dataclass. The runner is
`test_interference.py::test_scenario` which:

1. Loads `base_snapshot` and applies `overlay` via `fake_platform()`.
2. Wraps with `fake_actors(other_actors)` to make subprocess + service
   queries from actuator code report the right state.
3. Discovers actuators via the actual discovery code and inspects their
   `make()` / `supports()` results.
4. Asserts the observed outcome matches `expected`. `xfail` markers tag
   known gaps so the matrix is honest.

Why we patch subprocess.run globally here (not in fake_platform):
    actuators import `subprocess` at module-level. `fake_platform` patches
    only `coolstep.compat.detect.subprocess.run`. To make
    `asusctl_fan_curve._is_game_mode_active()` lie about game-mode, the
    patch needs to land in the asusctl module too. Doing this only for
    interference tests keeps the simpler hw-matrix harness untouched.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch


@dataclass
class Scenario:
    name: str
    title: str
    base_snapshot: str
    overlay: dict[str, Any] = field(default_factory=dict)
    other_actors: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    bugcase_status: str = "covered"   # "covered" | "gap" | "design"
    fix_proposal: str = ""
    upstream_reference: str = ""


@contextmanager
def fake_actors(
    other_actors: dict[str, str],
    asusctl_version: str = "6.1.2",
    ryzenadj_secure_boot_block: bool = False,
) -> Iterator[None]:
    """Patch subprocess.run + shutil.which globally for the duration.

    `other_actors`:
        "power-profiles-daemon.service" -> "active" | "inactive"
        "tlp.service"                   -> "active" | "inactive"
        "game-mode.service"             -> "active" | "inactive"
        "thermald.service"              -> "active" | "inactive"
        "gamemoded.service"             -> "active" | "inactive"  (Feral)
    """

    class _CP:
        def __init__(self, stdout=b"", stderr=b"", returncode=0, text=False):
            if text:
                stdout = stdout.decode() if isinstance(stdout, bytes) else stdout
                stderr = stderr.decode() if isinstance(stderr, bytes) else stderr
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode
            self.args = ()

    def fake_run(cmd, *args, **kwargs):
        text = bool(kwargs.get("text") or kwargs.get("universal_newlines"))
        if not isinstance(cmd, (list, tuple)):
            cmd = [cmd]
        cmd = list(cmd)
        # systemctl is-active <unit>
        if cmd[:2] == ["systemctl", "is-active"] or (
            cmd[:1] == ["systemctl"] and "is-active" in cmd
        ):
            unit = cmd[-1]
            state = other_actors.get(unit, "inactive")
            if state == "active":
                return _CP(stdout=b"active\n", returncode=0, text=text)
            return _CP(stdout=b"inactive\n", returncode=3, text=text)
        # asusctl --version / info
        if cmd[:1] == ["asusctl"]:
            if "--version" in cmd or "info" in cmd:
                if asusctl_version:
                    return _CP(stdout=f"asusctl {asusctl_version}\n".encode(), returncode=0, text=text)
                return _CP(stderr=b"unknown flag\n", returncode=2, text=text)
            # fan-curve etc.
            return _CP(returncode=0, text=text)
        # ryzenadj -i parse
        if cmd[:1] == ["ryzenadj"]:
            if ryzenadj_secure_boot_block:
                return _CP(
                    stdout=b"",
                    stderr=b"PCI Bus is not writeable, check secure boot\n",
                    returncode=1,
                )
            return _CP(
                stdout=(
                    b"STAPM LIMIT          |    45.000 |    7.500 | slow-limit\n"
                    b"PPT LIMIT FAST       |    65.000 |   12.345 | fast-limit\n"
                    b"PPT LIMIT SLOW       |    45.000 |    9.876 | slow-limit\n"
                ),
                returncode=0,
                text=text,
            )
        # sudo -n ryzenadj …
        if cmd[:2] == ["sudo", "-n"] and len(cmd) > 2 and cmd[2] == "ryzenadj":
            if ryzenadj_secure_boot_block:
                return _CP(stderr=b"secure boot lockdown\n", returncode=1)
            return _CP(returncode=0)
        # pacman -Q asusctl
        if cmd[:2] == ["pacman", "-Q"]:
            return _CP(
                stdout=f"{cmd[-1]} {asusctl_version}-1\n".encode(),
                returncode=0,
            )
        return _CP(returncode=127)

    def fake_service_active(name: str) -> bool:
        return other_actors.get(name) == "active"

    with patch("subprocess.run", side_effect=fake_run), \
         patch("coolstep.compat.detect._service_active", side_effect=fake_service_active):
        yield


# ============================================================================
# SCENARIOS — each is a real-world conflict pattern. `bugcase_status`:
#   "covered" — coolstep gates correctly (asserting the gate holds)
#   "gap"     — no gate today, test xfails as proof-of-gap (with fix proposal)
#   "design"  — separation by design (temp-range, facet, etc.), smoke-asserted
# ============================================================================

SCENARIOS: list[Scenario] = [

    # ------------------------------------------------------------------
    # S1. game-mode active + asusctl_fan_curve_bias — defer kicks in.
    # ------------------------------------------------------------------
    Scenario(
        name="s01-game-mode-defers-asusctl",
        title="game-mode.service active → asusctl_fan_curve_bias defers (COVERED)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={"game-mode.service": "active"},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1", "COOLSTEP_GAME_MODE_DEFER": "1"},
        expected={
            "asusctl_supports_ramp_cooling": False,
            "game_mode_optimizer_supports_ramp_cooling": True,
            "explanation": "default defer: game-mode owner replaces asusctl bias",
        },
        bugcase_status="covered",
        upstream_reference="docs/game-mode-coordination.md + asusctl_fan_curve.py:567-605",
    ),

    # ------------------------------------------------------------------
    # S2. game-mode active + DEFER=0 (cooperative) — both fire (stress test).
    # ------------------------------------------------------------------
    Scenario(
        name="s02-game-mode-cooperative",
        title="DEFER=0 → asusctl bias AND game_mode_optimizer both stay quiet (cooperative)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={"game-mode.service": "active"},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1", "COOLSTEP_GAME_MODE_DEFER": "0"},
        expected={
            "asusctl_supports_ramp_cooling": True,
            "game_mode_optimizer_supports_ramp_cooling": False,
            "explanation": "cooperative mode hands ownership back to asusctl bias",
        },
        bugcase_status="covered",
        upstream_reference="game_mode_optimizer.py:91-104 + asusctl_fan_curve.py:_defer_to_game_mode",
    ),

    # ------------------------------------------------------------------
    # S3. PPD active + epp_shift — gate added 2026-05-12, CLOSED.
    # ------------------------------------------------------------------
    Scenario(
        name="s03-ppd-vs-epp-shift",
        title="power-profiles-daemon active → epp_shift.make() cedes EPP (COVERED)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={"power-profiles-daemon.service": "active"},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "explanation": (
                "epp_shift.make() now reads caps.power_profiles_daemon_active "
                "and returns None to cede EPP ownership. Override available "
                "via COOLSTEP_EPP_SHIFT_FORCE=1 for users who want to fight PPD."
            ),
        },
        bugcase_status="covered",
        upstream_reference="epp_shift.py::make() 2026-05-12 gate",
    ),

    # ------------------------------------------------------------------
    # S4. TLP active + ryzenadj_cap — gate added 2026-05-12, CLOSED.
    # ------------------------------------------------------------------
    Scenario(
        name="s04-tlp-vs-ryzenadj",
        title="TLP active → ryzenadj_cap.make() cedes STAPM/PPT (COVERED)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={"tlp.service": "active"},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "explanation": (
                "ryzenadj_cap.make() now reads caps.tlp_active and returns "
                "None. Override via COOLSTEP_RYZENADJ_FORCE=1."
            ),
        },
        bugcase_status="covered",
        upstream_reference="ryzenadj_cap.py::make() 2026-05-12 gate",
    ),

    # ------------------------------------------------------------------
    # S5. Secure boot ON + ryzenadj — actuator activates but write silently fails.
    # ------------------------------------------------------------------
    Scenario(
        name="s05-secure-boot-ryzenadj-silent-noop",
        title="Secure boot enabled → ryzenadj loads but write fails on PCI bus (PARTIAL)",
        base_snapshot="thinkpad_x1_carbon_i7_intel",   # uses secure_boot=True
        overlay={
            "which": {"ryzenadj": True},   # binary installed by user
            # but the host is Intel — ryzenadj is irrelevant. We borrow this
            # snapshot just for secure_boot=True; the actuator wouldn't fire
            # anyway because cpu_vendor=intel. Use a TUF overlay instead.
        },
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "ryzenadj_cap_make_returns": "None or instance with secure-boot warning",
            "note": "On secure-boot=True + AMD, ryzenadj -i fails; make() returns None",
        },
        bugcase_status="design",
        upstream_reference="ryzenadj_cap.py:38-49 secure-boot blocker docstring",
    ),

    # ------------------------------------------------------------------
    # S6. asusctl v5 (API break) — version_ok=False, asusctl-based actuators don't load.
    # ------------------------------------------------------------------
    Scenario(
        name="s06-asusctl-v5-api-break",
        title="asusctl v5 installed (pre-v6 CLI shape) → both asusctl actuators skip (COVERED)",
        base_snapshot="asus_tuf_a15_7940hs",
        overlay={"asusctl_version": "5.2.0"},
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "caps_asusctl_version_ok": False,
            "asusctl_fan_curve_make_returns_none": True,
            "game_mode_optimizer_make_returns_none": True,
            "explanation": "MIN_ASUSCTL_MAJOR=6 gates make() in both actuators",
        },
        bugcase_status="covered",
        upstream_reference="game_mode_optimizer.py:49 MIN_ASUSCTL_MAJOR=6",
    ),

    # ------------------------------------------------------------------
    # S7. Server facet + IPMI — actuators should not auto-arm (facet design).
    # ------------------------------------------------------------------
    Scenario(
        name="s07-server-facet-suppresses-actuators",
        title="facet=server → install plan disables actuators (BMC owns chassis)",
        base_snapshot="dell_poweredge_r750_server",
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},  # user enabled but facet should still suppress
        expected={
            "caps_facet": "server",
            "plan_env_suggestions_disable_actuator": True,
            "explanation": "install_plan.py forces COOLSTEP_ACTUATOR_ENABLE=false for server facet",
        },
        bugcase_status="design",
        upstream_reference="install_plan.py:623-634",
    ),

    # ------------------------------------------------------------------
    # S8. Two coolstep instances → both touch EPP, no inter-process lock (GAP).
    # ------------------------------------------------------------------
    Scenario(
        name="s08-two-coolstep-instances",
        title="Two coolstep processes both arm epp_shift → race, no mutex (GAP)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "epp_shift_uses_inter_process_lock": False,  # <-- gap
            "race_outcome": (
                "Both instances snapshot the same baseline at startup; both "
                "apply within the same tick; both revert independently. "
                "Net effect: arbitrary EPP per cpu if applies interleave."
            ),
        },
        bugcase_status="gap",
        fix_proposal=(
            "Add filesystem lock /run/coolstep.epp.lock acquired by EppShift.__init__ "
            "via fcntl.LOCK_EX|LOCK_NB. If acquire fails, make() returns None."
        ),
        upstream_reference="No inter-process coordination anywhere in P2.5",
    ),

    # ------------------------------------------------------------------
    # S9. thermald running + coolstep — temperature-range separation (DESIGN).
    # ------------------------------------------------------------------
    Scenario(
        name="s09-thermald-temp-range-design",
        title="thermald.service active → coolstep coexists by operating at 82°C soft threshold",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={"thermald.service": "active"},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "thermald_owns_hard_throttle_at": "~95°C",
            "coolstep_owns_soft_margin_at": "~82°C",
            "coordination_mechanism": "temperature-range, not service-probe",
            "no_probe_today": True,
        },
        bugcase_status="design",
        upstream_reference="docs/curve-ownership.md:13-15 + daemon.py:58-60",
    ),

    # ------------------------------------------------------------------
    # S10. Hyprland stale HYPRLAND_INSTANCE_SIGNATURE — old daemon detects wrong session.
    # ------------------------------------------------------------------
    Scenario(
        name="s10-hyprland-stale-signature",
        title="HYPRLAND_INSTANCE_SIGNATURE points at dead session → socket probe must fall through",
        base_snapshot="asus_tuf_a15_7940hs",
        overlay={
            "env": {
                "HYPRLAND_INSTANCE_SIGNATURE": "1700000000_dead_session",
            },
            "hypr_socket": None,   # no actual socket file on disk
        },
        other_actors={},
        env={},
        expected={
            "compositor_still_detected_as_hyprland": True,
            "note": (
                "Env var alone is enough; detect.py:163 returns 'hyprland'. "
                "Real socket check exists in _hyprland_socket_exists but only "
                "as fallback. Stale env can lead hyprctl_available to be True "
                "but every hyprctl call fails — a known gotcha logged elsewhere."
            ),
        },
        bugcase_status="gap",
        fix_proposal=(
            "detect.py:164: also verify socket file exists before returning 'hyprland'. "
            "If env says hyprland but socket missing -> emit warning and return 'unknown'."
        ),
        upstream_reference="memory/feedback_hyprctl_sig_rotation.md",
    ),

    # ------------------------------------------------------------------
    # S11. RAPL CVE-2020-8694 — counters present but unreadable as user.
    # ------------------------------------------------------------------
    Scenario(
        name="s11-rapl-cve-2020-8694",
        title="RAPL energy_uj present but root-only → rapl_energy collector skips, warning emitted",
        base_snapshot="dell_poweredge_r750_server",  # already has rapl_readable=False
        other_actors={},
        env={},
        expected={
            "caps_rapl_powercap": True,
            "caps_rapl_readable": False,
            "warning_mentions_cve": True,
            "plan_includes_rapl_perm_pointer": True,
        },
        bugcase_status="covered",
        upstream_reference="detect.py:408-412 + install_plan.py rapl_perm pointer",
    ),

    # ------------------------------------------------------------------
    # S12. Battery + asusctl absent — laptop without fan control tool.
    # ------------------------------------------------------------------
    Scenario(
        name="s12-laptop-no-fan-tool",
        title="Laptop (battery present) + no asusctl/nbfc/thinkfan → warning + community pointers",
        base_snapshot="thinkpad_x1_carbon_i7_intel",  # has BAT0, no asusctl
        other_actors={},
        env={},
        expected={
            "warning_mentions_fan_control_tool": True,
            "community_pointers_count_min": 1,
        },
        bugcase_status="covered",
        upstream_reference="detect.py:397-398",
    ),

    # ------------------------------------------------------------------
    # S13. Feral gamemoded vs Hyprland game-mode.service — namespace miss.
    # ------------------------------------------------------------------
    Scenario(
        name="s13-feral-gamemoded-missed",
        title="Feral gamemoded.service active (system unit, not --user) → coolstep doesn't see it (GAP)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={
            "gamemoded.service": "active",          # system unit (Feral)
            "game-mode.service": "inactive",        # user unit (Hyprland-driven)
        },
        env={"COOLSTEP_ACTUATOR_ENABLE": "1", "COOLSTEP_GAME_MODE_DEFER": "1"},
        expected={
            "asusctl_supports_ramp_cooling": True,
            "explanation": (
                "asusctl_fan_curve._is_game_mode_active() runs `systemctl --user "
                "is-active game-mode.service`. It misses Feral's system-level "
                "`gamemoded.service` entirely — bias fires while Feral is also "
                "running, leading to two actors clobbering EC fan profile."
            ),
        },
        bugcase_status="covered",
        upstream_reference="asusctl_fan_curve.py + game_mode_optimizer.py probe gamemoded.service since 2026-05-12",
    ),

    # ------------------------------------------------------------------
    # S14. nvidia-smi persistence mode — GPU "idle" power inflated.
    # ------------------------------------------------------------------
    Scenario(
        name="s14-nvidia-persistence-mode",
        title="nvidia-smi -pm 1 persistence daemon → idle GPU draws 25W not 6W (GAP)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={"nvidia-persistenced.service": "active"},
        env={},
        expected={
            "explanation": (
                "Persistence mode prevents NVIDIA driver from unloading on "
                "idle. GPU idle power becomes ~25W instead of ~6W on RTX 4060M. "
                "coolstep's NVML collector reads the inflated value; "
                "thermal-efficiency calibration ends up biased pessimistic. "
                "No detection of nvidia-persistenced state exists in caps."
            ),
            "collector_aware_of_persistence_mode": False,
        },
        bugcase_status="gap",
        fix_proposal=(
            "Add caps.nvidia_persistence_mode (probe via `nvidia-smi -q | "
            "grep 'Persistence Mode'` or `pynvml.nvmlDeviceGetPersistenceMode`). "
            "Collector emits a `persistence_mode_warning` signal when active "
            "so calibration baselines can be adjusted."
        ),
        upstream_reference="Not in detect.py or any collector",
    ),

    # ------------------------------------------------------------------
    # S15. asusctl baseline drift — user manually changes curve in 300s window.
    # ------------------------------------------------------------------
    Scenario(
        name="s15-asusctl-baseline-drift",
        title="User edits fan-curve via GUI within 300s of coolstep snapshot → revert cedes to user curve (COVERED 2026-07-10)",
        base_snapshot="asus_tuf_a15_7940hs",
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "explanation": (
                "asusctl_fan_curve._baseline_ttl_s = 300. coolstep snapshots "
                "current curve at t=0; user edits the curve mid-window. "
                "revert() now re-reads the live curve first: if it matches "
                "neither the last issued curve nor the saved baseline, the "
                "user took ownership — restore is skipped, the stale "
                "baseline is dropped (next apply re-snapshots), and the "
                "journal records the cession. _ensure_baseline() also "
                "refuses to adopt our own issued curve as baseline when the "
                "TTL lapses while a bias is applied."
            ),
            "baseline_drift_detection": True,
        },
        bugcase_status="covered",
        upstream_reference="asusctl_fan_curve.py:revert + _ensure_baseline (S15 guards)",
    ),

    # ------------------------------------------------------------------
    # S16. ryzenadj sudoers entry missing — apply() silently fails.
    # ------------------------------------------------------------------
    Scenario(
        name="s16-ryzenadj-sudoers-missing",
        title="ryzenadj installed but sudoers entry missing → apply() rc=1, no telemetry alert (PARTIAL)",
        base_snapshot="asus_tuf_a15_7940hs",
        overlay={
            "which": {"ryzenadj": True, "sudo": True},
        },
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},
        expected={
            "explanation": (
                "ryzenadj_cap docstring tells the user to install a NOPASSWD "
                "sudoers snippet. If they don't, every apply() invocation "
                "via `sudo -n` returns rc=1 with 'a password is required'. "
                "ActionResult.error captures this but no dashboard surface "
                "consumes the signal to nag the user. Effectively the actuator "
                "is a no-op forever and the user never finds out."
            ),
            "sudoers_install_check": False,
        },
        bugcase_status="covered",
        upstream_reference="ryzenadj_cap.py::_sudoers_preflight() since 2026-05-12",
    ),

    # ------------------------------------------------------------------
    # S17. BMC overrides host — server facet, user enables actuators anyway.
    # ------------------------------------------------------------------
    Scenario(
        name="s17-bmc-overrides-host",
        title="Server facet + user enables actuators anyway → BMC silently revokes (DESIGN gap)",
        base_snapshot="dell_poweredge_r750_server",
        overlay={
            "epp": {"present": True, "value": "performance", "writable": True},  # user tinkered with kernel cmdline
        },
        other_actors={},
        env={"COOLSTEP_ACTUATOR_ENABLE": "1"},  # user overrode facet recommendation
        expected={
            "caps_facet": "server",
            "plan_disables_actuator_anyway": True,
            "explanation": (
                "install_plan still emits COOLSTEP_ACTUATOR_ENABLE=false for "
                "server facet. But a determined user can override the env var. "
                "If they do: BMC firmware polls thermal sensors every ~2s and "
                "asserts its own EPP/governor policy via Mngmt Engine writes, "
                "bypassing coolstep entirely. ActionResult.applied succeeds "
                "but the change lives ~2s before BMC revokes."
            ),
        },
        bugcase_status="design",
        upstream_reference="install_plan.py:623-634 + Dell iDRAC9 docs",
    ),
]
