"""Interference test pillar.

The user emphasised this is the main concern (hyprland/distro/cpu/gpu being
sideshow). Coolstep does not live in a vacuum — it shares hardware control
surfaces with power-profiles-daemon, TLP, asusctl, thermald, game-mode.service,
nvidia-smi-persistenced, Feral gamemode, BMC firmware, and (most evilly)
another coolstep instance.

Each scenario in `_scenarios.py` declares:
    base_snapshot   — hardware fixture
    other_actors    — dict of "service" → state ("active"/"inactive")
    env             — additional env vars (gate flags)
    expected        — what coolstep SHOULD do; if missing, that's the gap
    bugcase_status  — "covered" | "gap" | "design" — drives xfail markers
    fix_proposal    — concrete code change if status=="gap"

`test_interference.py` parametrises all scenarios and asserts current
behaviour matches `expected`. xfail markers tag known gaps so they surface
in pytest output without blocking CI — the matrix is honest about what is
covered vs. open.
"""

from tests.compat.interference._scenarios import (
    SCENARIOS,
    Scenario,
    fake_actors,
)

__all__ = ["SCENARIOS", "Scenario", "fake_actors"]
