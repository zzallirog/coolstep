# Interference matrix — coolstep vs. concurrent actors

> **TL;DR.** The hardware/distro matrix is a sideshow. The real concern is
> *interference* — coolstep does not own the thermal/power surface alone.
> power-profiles-daemon, TLP, asusctl, thermald, game-mode.service, Feral
> gamemoded, BMC firmware, nvidia-smi-persistenced and (worst of all) a
> second coolstep instance all touch overlapping knobs. This file is the
> coverage map.

## Actors

| Actor | Surface it owns | How coolstep can collide |
|---|---|---|
| **power-profiles-daemon** | `/sys/.../cpufreq/energy_performance_preference`, `/sys/firmware/acpi/platform_profile`, governor | `epp_shift` writes the same path |
| **TLP** | EPP, STAPM/PPT (AMD), governor, runtime PM | `epp_shift`, `ryzenadj_cap`, `cap_boost` |
| **auto-cpufreq** | governor | `epp_shift` indirectly via governor change |
| **thermald** (Intel) | kernel thermal trip, throttling | none directly — temperature-range separation |
| **game-mode.service** (Hyprland-event-driven) | asusctl fan curve mode flag | `asusctl_fan_curve_bias` |
| **gamemoded** (Feral) | GPU clock, CPU governor, niceness | `epp_shift`, `ryzenadj_cap` (untested) |
| **asusctl daemon** | EC fan curves, AURA, platform profile | `asusctl_fan_curve_bias`, `game_mode_optimizer` |
| **nvidia-smi -pm** | GPU persistence, power limits | currently unobserved — read-only collector only |
| **BMC firmware** (IPMI/Redfish) | chassis fans, throttle | `redfish`/`ipmi` collectors observe-only; server facet suppresses actuators |
| **Another coolstep instance** | same actuators | no inter-process lock today |

## Scenario catalogue

Lives in `tests/compat/interference/_scenarios.py`. **17 scenarios**
(S01-S17, see table below), each parametrized through
`test_interference.py`. Each `Scenario` declares `bugcase_status` ∈ {`covered`, `gap`, `design`}:

- **covered** — coolstep has an explicit gate; test asserts the gate
  holds. If someone refactors and breaks the gate, the test fails.
- **gap** — no gate today; test asserts the absence (proof-of-gap) and
  carries a `fix_proposal`. When someone implements the fix, the
  assertion flips and the test fails — forcing a status update.
- **design** — separation is structural (temperature range, facet
  suppression). Smoke-asserted; never expected to "close".

| # | Scenario | Status | What it proves |
|---|---|---|---|
| S01 | game-mode active → asusctl bias defers | ✅ covered | Default `COOLSTEP_GAME_MODE_DEFER=1` flips `asusctl_fan_curve_bias.supports()` to False, hands ownership to `game_mode_optimizer` |
| S02 | game-mode active + `DEFER=0` cooperative | ✅ covered | Truth table inverts: asusctl bias regains support, game_mode_optimizer steps back |
| S03 | PPD active + epp_shift fires | ✅ **closed 2026-05-12** | `epp_shift.make()` cedes when `caps.power_profiles_daemon_active`; override `COOLSTEP_EPP_SHIFT_FORCE=1` |
| S04 | TLP active + ryzenadj_cap fires | ✅ **closed 2026-05-12** | `ryzenadj_cap.make()` cedes when `caps.tlp_active`; override `COOLSTEP_RYZENADJ_FORCE=1` |
| S05 | secure boot + ryzenadj | ⚙ design | Documented in `ryzenadj_cap.py:38-49`; actuator is dry-run-equivalent until `ryzen_smu` DKMS or secure-boot disabled |
| S06 | asusctl v5 (pre-v6 API) | ✅ covered | `MIN_ASUSCTL_MAJOR=6` gates `make()` in both asusctl actuators |
| S07 | server facet | ⚙ design | `install_plan.py:623-634` forces `COOLSTEP_ACTUATOR_ENABLE=false`; BMC owns fans |
| S08 | Two coolstep instances on same EPP | ❌ **gap** | No fcntl lock; both arm/revert independently, EPP can end in arbitrary state |
| S09 | thermald active + coolstep | ⚙ design | Temperature-range separation: thermald at hard ~95°C trip, coolstep at soft ~82°C — no service probe |
| S10 | Hyprland stale `HYPRLAND_INSTANCE_SIGNATURE` | ❌ **gap** | env-var alone determines compositor; if session died, every hyprctl call fails downstream |
| S11 | RAPL CVE-2020-8694 (root-only) | ✅ covered | Warning emitted, `rapl_energy` collector skips, community pointer surfaces |
| S12 | Laptop without any fan-control tool | ✅ covered | `has_battery && !asusctl && !nbfc && …` triggers warning + community pointers |
| S13 | Feral `gamemoded.service` vs Hyprland `game-mode.service` | ✅ **closed 2026-05-12** | Both `asusctl_fan_curve._is_game_mode_active` + `game_mode_optimizer._is_game_mode_active` now probe `gamemoded.service` (system unit) alongside `--user game-mode.service` |
| S14 | `nvidia-persistenced.service` keeps GPU driver loaded → idle GPU draws 25W not 6W | ❌ **gap** | No `caps.nvidia_persistence_mode` field; calibration baselines biased pessimistic |
| S15 | User edits asusctl curve via ROG Control Center within 300s window | ✅ **closed 2026-07-10** | Two guards: `revert()` re-reads the live curve first and cedes to an external user edit instead of clobbering it; `_ensure_baseline()` refuses to adopt coolstep's own issued curve as baseline |
| S16 | `ryzenadj` installed but sudoers `NOPASSWD` entry missing | ✅ **closed 2026-05-12** | `ryzenadj_cap._sudoers_preflight()` runs `sudo -n /usr/bin/ryzenadj --version` at startup; refuses to load if rc≠0 + logs the exact snippet to install |
| S17 | Server facet user-overrides `COOLSTEP_ACTUATOR_ENABLE=1` | ⚙ design | BMC firmware revokes any write within ~2s; install_plan still emits the disable env, user opts in at own risk |

### Summary (updated 2026-07-10)

**10 covered / 3 open / 4 design.** 4 gaps closed 2026-05-12 by adding
real code gates (S03, S04, S13, S16) — see git log + `epp_shift.py::make()`,
`ryzenadj_cap.py::_sudoers_preflight()`, `_is_game_mode_active()` in both
asusctl actuators. S15 (asusctl baseline drift) closed 2026-07-10 with
the `revert()` live-curve re-read + `_ensure_baseline()` self-curve
refusal. Remaining open: S08 inter-process lock, S10 Hyprland
socket verify, S14 nvidia persistence mode.
Each is tracked in `TODO.md` under "🛡️ INTERFERENCE GATES".

## How to read the matrix

A scenario marked **gap** is not a regression — it's a documented open
issue that the harness can detect mechanically. The `fix_proposal` field
gives the minimal change set that would close the gap; the test then
needs its `bugcase_status` updated to `covered` and its assertion
inverted.

## Adding a scenario

1. Pick the conflict. Pair = (coolstep actuator/collector, external actor).
2. Add a `Scenario(…)` entry in `_scenarios.py` — point to a base
   `snapshot_name`, add `other_actors` mapping for systemctl is-active,
   set `env` for any flag, declare `expected`.
3. Implement the per-name branch in `test_interference.py::_assert_outcome`
   — assert what you actually want verified.
4. If the scenario reveals an open gap, set `bugcase_status="gap"` and
   write a one-paragraph `fix_proposal`.
5. Re-run `pytest tests/compat/interference/ -v`.

## Why this is the main pillar (not the hw matrix)

The hw matrix proves *coolstep doesn't crash on platform X*. Useful, but
shallow — the detect/install layer is mostly mechanical string-matching
of `/sys` paths. The interference matrix proves *coolstep doesn't fight
other daemons that already own pieces of the same surface*. That is where
real users get burned: their PPD profile flips back 800 ms after coolstep
biases EPP; their TLP-set STAPM cap is overwritten by ryzenadj; their
asusctl custom curve gets clobbered by a 30s revert from coolstep that
they never asked for.

The hw matrix has no users; the interference matrix has every user.
