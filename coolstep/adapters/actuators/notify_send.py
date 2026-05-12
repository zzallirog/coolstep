"""notify-send actuator — surfaces NOTIFY_USER actions as desktop popups.

Wraps libnotify's `notify-send` CLI. Discovery: binary on PATH + DBus session
accessible (we trust notify-send to handle that). Single env knob:
COOLSTEP_NOTIFY_ENABLE — default "true". Set to "false" to disable entirely
(make() returns None, actuator absent from registry).

Cooldown: don't spam the user — if last apply() was < 30s ago for the same
verb, skip. Prevents tick-by-tick popup avalanche under sustained load.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

from coolstep.adapters.actuators._base import append_journal_event
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)

ENABLE_ENV = "COOLSTEP_NOTIFY_ENABLE"
COOLDOWN_S = float(os.environ.get("COOLSTEP_NOTIFY_COOLDOWN_S", 30.0))
SUBPROCESS_TIMEOUT_S = 1.5
APP_NAME = "coolstep"


class NotifySendActuator:
    name = "notify_send"

    def __init__(self, binary: str = "notify-send") -> None:
        self._binary = binary
        self._last_fire: dict[ActionVerb, float] = {}  # verb → ts

    def supports(self, verb: ActionVerb) -> bool:
        return verb == ActionVerb.NOTIFY_USER

    def dry_run(self, action: Action) -> SimResult:
        return SimResult(
            expected_effect={"notification_emitted": 1.0},
            confidence=0.95,
            reverts_in=0.0,  # notifications don't revert
        )

    def apply(self, action: Action) -> ActionResult:
        now = time.time()
        last = self._last_fire.get(action.verb, 0.0)
        if now - last < COOLDOWN_S:
            # In cooldown — silently skip, still journal as «cooldown»
            result = ActionResult(
                applied_at=now, cmd_executed=None,
                stdout_tail=f"COOLDOWN ({COOLDOWN_S:.0f}s gate, {now-last:.1f}s since last)",
            )
            append_journal_event({
                "kind": "apply", "actuator": self.name,
                "applied_at": now, "cmd_executed": None,
                "stdout_tail": result.stdout_tail, "error": None,
                "verb": action.verb.value, "params": dict(action.params),
                "expires_at": action.expires_at,
            })
            return result

        message = str(action.params.get("message", "coolstep: thermal pressure predicted"))
        urgency = str(action.params.get("urgency", "normal"))
        if urgency not in {"low", "normal", "critical"}:
            urgency = "normal"
        cmd = [
            self._binary,
            "--app-name", APP_NAME,
            "--urgency", urgency,
            "--icon", "weather-clear-warning",  # generic libnotify icon, falls back if absent
            "coolstep",
            message,
        ]
        cmd_str = " ".join(cmd)
        try:
            subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            self._last_fire[action.verb] = now
            result = ActionResult(
                applied_at=now, cmd_executed=cmd_str,
                stdout_tail=f"notification sent ({urgency})",
            )
            log.info("notify_send: %s [%s]", message, urgency)
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or b"")[-200:].decode("utf-8", errors="replace")
            result = ActionResult(
                applied_at=now, cmd_executed=cmd_str,
                stdout_tail=tail, error=f"returncode={exc.returncode}",
            )
            log.warning("notify_send FAILED (%d): %s", exc.returncode, tail)
        except (subprocess.TimeoutExpired, OSError) as exc:
            result = ActionResult(
                applied_at=now, cmd_executed=cmd_str,
                stdout_tail="", error=type(exc).__name__,
            )
            log.warning("notify_send TIMEOUT/OSError: %r", exc)
        # Journal event
        append_journal_event({
            "kind": "apply", "actuator": self.name,
            "applied_at": result.applied_at,
            "cmd_executed": result.cmd_executed,
            "stdout_tail": result.stdout_tail, "error": result.error,
            "verb": action.verb.value, "params": dict(action.params),
            "expires_at": action.expires_at,
        })
        return result

    def revert(self) -> None:
        # Notifications don't revert.
        return None


def make() -> NotifySendActuator | None:
    if os.environ.get(ENABLE_ENV, "true").lower() in {"false", "0"}:
        return None
    if shutil.which("notify-send") is None:
        return None
    return NotifySendActuator()
