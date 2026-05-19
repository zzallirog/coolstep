"""game-mode optimizer — dedicated hardware owner during game-mode.service.

When game-mode is active AND the user hasn't opted into cooperative biasing
(``COOLSTEP_GAME_MODE_DEFER=1`` — the default), this actuator becomes the sole
``RAMP_COOLING`` fire path. ``asusctl_fan_curve_bias`` stays silent (its
existing defer logic), so exactly one (or zero, under cooperative mode)
actuator handles the verb at any given moment.

Bias is a fixed game-profile curve — slightly more aggressive than
quietify-mid in the 73-78°C band, matching a typical Steam workload's heat
signature on the target hardware (Ryzen 9 7940HS).

ADR-015 P2.5 minimal first version: no KNN-driven intensity here. Game-mode
itself is the strongest predictor; we bias up unconditionally while it's on,
revert when game-mode goes inactive (per TTL or detected via ``supports()``
flip).

Discovery contract:
    1. ``asusctl`` binary on PATH
    2. ``asusctl info`` reports v6+ (4.x/5.x CLI flag shapes differ)
    3. ``COOLSTEP_ACTUATOR_ENABLE`` ≠ "false" / "0"   (hard kill switch)

``apply()`` writes to hardware only if ``COOLSTEP_ACTUATOR_ENABLE`` ∈
{"true","1"}. Default is "dry-run" — logs the cmd, marks ActionResult with
``stdout_tail="DRY-RUN game-profile"``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Iterable
from threading import RLock

from coolstep.adapters.actuators._base import append_journal_event
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)

DEFAULT_PROFILE = os.environ.get("COOLSTEP_ASUSCTL_PROFILE", "Performance")
DEFAULT_FAN = os.environ.get("COOLSTEP_ASUSCTL_FAN", "cpu")
ENABLE_ENV = "COOLSTEP_ACTUATOR_ENABLE"
SUBPROCESS_TIMEOUT_S = 2.0
MIN_ASUSCTL_MAJOR = 6
GM_CACHE_TTL_S = 5.0

# Game-profile curve — more aggressive than quietify-mid in the 73-78°C band.
# Anchors temp:pwm chosen for sustained Steam workloads on Ryzen 9 7940HS.
GAME_PROFILE_ANCHORS: list[tuple[int, int]] = [
    (60, 5), (65, 15), (70, 30), (73, 50),
    (76, 70), (80, 85), (85, 95), (90, 100),
]


def _format_data_arg(anchors: Iterable[tuple[int, int]]) -> str:
    """``asusctl --data`` accepts ``Xc:Y%``. Integer °C + integer PWM%."""
    return ",".join(f"{int(round(t))}c:{int(round(p))}%" for t, p in anchors)


class GameModeOptimizer:
    """Dedicated RAMP_COOLING owner while game-mode.service is active.

    State that survives across ``apply()`` calls:
      _journal   — last N ActionResult for dashboard tile
      _gm_cache  — (is_active: bool, monotonic_ts: float) | None
    """

    name = "game_mode_optimizer"

    def __init__(
        self,
        profile: str = DEFAULT_PROFILE,
        fan: str = DEFAULT_FAN,
        binary: str = "asusctl",
        journal_capacity: int = 64,
    ) -> None:
        self._profile = profile
        self._fan = fan
        self._binary = binary
        self._journal: deque[ActionResult] = deque(maxlen=journal_capacity)
        self._lock = RLock()
        self._gm_cache: tuple[bool, float] | None = None

    # ── public ────────────────────────────────────────────────────────────

    def supports(self, verb: ActionVerb) -> bool:
        """Owns RAMP_COOLING only when game-mode is active + defer-mode is on.

        Truth table for RAMP_COOLING (other verbs always False):
          inactive game-mode          → False   (asusctl_fan_curve_bias owns)
          active + DEFER=1 (default)  → True    (THIS actuator owns)
          active + DEFER=0            → False   (asusctl_fan_curve_bias owns, cooperative)
        """
        if verb != ActionVerb.RAMP_COOLING:
            return False
        if not self._is_game_mode_active():
            return False
        defer = os.environ.get("COOLSTEP_GAME_MODE_DEFER", "1").lower()
        return defer not in {"0", "false"}

    def dry_run(self, action: Action) -> SimResult:
        return SimResult(
            expected_effect={
                "cpu_temp_delta_c": -3.0,   # empirical vs Performance default
                "profile": "game",          # marker — not a numeric effect
            },
            confidence=0.6,
            reverts_in=max(0.0, action.expires_at - time.time()),
        )

    def apply(self, action: Action) -> ActionResult:
        cmd = [
            self._binary, "fan-curve",
            "--mod-profile", self._profile,
            "--fan", self._fan,
            "--data", _format_data_arg(GAME_PROFILE_ANCHORS),
        ]
        cmd_str = " ".join(cmd)
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()
        if mode not in {"true", "1"}:
            result = ActionResult(
                applied_at=time.time(),
                cmd_executed=cmd_str,
                stdout_tail="DRY-RUN game-profile",
            )
            log.info("%s DRY-RUN: %s", self.name, cmd_str)
            self._record(result, action)
            return result
        try:
            cp = subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            tail = cp.stdout[-200:].decode("utf-8", errors="replace") if cp.stdout else ""
            result = ActionResult(
                applied_at=time.time(),
                cmd_executed=cmd_str, stdout_tail=tail,
            )
            log.info("%s APPLIED: %s", self.name, cmd_str)
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or b"")[-200:].decode("utf-8", errors="replace")
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=tail, error=f"returncode={exc.returncode}",
            )
            log.warning("%s FAILED (%d): %s", self.name, exc.returncode, tail)
        except (subprocess.TimeoutExpired, OSError) as exc:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail="", error=type(exc).__name__,
            )
            log.warning("%s TIMEOUT/OSError: %r", self.name, exc)
        self._record(result, action)
        return result

    def revert(self) -> None:
        """Reset to asusctl factory ``--default`` for the active profile.

        game-mode itself owns the Performance profile lifecycle; when this
        revert fires, game-mode is also exiting, so reverting via
        ``--default`` is safe — the user's own curve gets re-established by
        whatever sets it after game-mode (or stays at default until the next
        manual tune).
        """
        cmd = [
            self._binary, "fan-curve",
            "--mod-profile", self._profile, "--default",
        ]
        cmd_str = " ".join(cmd)
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()
        if mode not in {"true", "1"}:
            log.info("%s DRY-RUN revert: %s", self.name, cmd_str)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": "DRY-RUN game-profile end",
                "cmd_executed": cmd_str,
            })
            return
        try:
            subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            log.info("%s revert: %s", self.name, cmd_str)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": "game-profile end",
                "cmd_executed": cmd_str,
            })
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("%s revert failed: %r", self.name, exc)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": "game-profile end",
                "cmd_executed": cmd_str,
                "error": type(exc).__name__,
            })

    def journal(self) -> list[ActionResult]:
        with self._lock:
            return list(self._journal)

    # ── internal ──────────────────────────────────────────────────────────

    def _record(self, result: ActionResult, action: Action) -> None:
        with self._lock:
            self._journal.append(result)
        append_journal_event({
            "kind": "apply",
            "actuator": self.name,
            "applied_at": result.applied_at,
            "cmd_executed": result.cmd_executed,
            "stdout_tail": result.stdout_tail,
            "error": result.error,
            "verb": action.verb.value,
            "params": dict(action.params),
            "expires_at": action.expires_at,
        })

    def _is_game_mode_active(self) -> bool:
        """Probe game-mode.service (--user) or gamemoded.service (system) — 5 s cache.

        S13 in interference-matrix.md: Feral gamemoded ships as a system unit
        named `gamemoded.service`. Without probing it, this optimizer thinks
        game-mode is inactive and asusctl_fan_curve_bias re-arms — two actors
        clobber the EC fan profile.
        """
        now = time.monotonic()
        if self._gm_cache is not None:
            cached_active, cached_at = self._gm_cache
            if now - cached_at < GM_CACHE_TTL_S:
                return cached_active

        def _probe(args: list[str]) -> bool:
            try:
                cp = subprocess.run(
                    args, capture_output=True, timeout=0.5, check=False,
                )
                return cp.returncode == 0 and cp.stdout.strip() == b"active"
            except Exception:  # noqa: BLE001
                return False

        is_active = (
            _probe(["systemctl", "--user", "is-active", "game-mode.service"])
            or _probe(["systemctl", "is-active", "gamemoded.service"])
        )
        self._gm_cache = (is_active, now)
        return is_active


def _host_supported(binary: str = "asusctl") -> bool:
    """Cheap discovery: binary on PATH + asusctl v6+."""
    if shutil.which(binary) is None:
        return False
    try:
        cp = subprocess.run(
            [binary, "info"], capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if cp.returncode != 0:
        return False
    m = re.search(r"v?(\d+)\.\d+\.\d+", cp.stdout.decode("utf-8", errors="replace"))
    return bool(m) and int(m.group(1)) >= MIN_ASUSCTL_MAJOR


def make() -> GameModeOptimizer | None:
    if os.environ.get(ENABLE_ENV, "dry-run").lower() in {"false", "0"}:
        return None
    if not _host_supported():
        return None
    return GameModeOptimizer()
