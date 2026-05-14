"""Readonly actuator — logs intent only, no hardware writes.

Default actuator in P0. Dashboard's «Action log» tile reads its in-memory
journal so user sees what the system *would* have done before any real
hardware writes are enabled.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Iterable
from threading import RLock

from coolstep.adapters.actuators._base import append_journal_event
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)


class ReadonlyActuator:
    name = "readonly_log"

    def __init__(self, journal_capacity: int = 256) -> None:
        self._journal: deque[ActionResult] = deque(maxlen=journal_capacity)
        self._intents: deque[Action] = deque(maxlen=journal_capacity)
        self._lock = RLock()

    def supports(self, verb: ActionVerb) -> bool:
        return True  # logs every verb

    def dry_run(self, action: Action) -> SimResult:
        return SimResult(
            expected_effect={"applied": 0.0, "would_apply": 1.0},
            confidence=1.0,
            reverts_in=max(0.0, action.expires_at - time.time()),
        )

    def apply(self, action: Action) -> ActionResult:
        result = ActionResult(
            applied_at=time.time(),
            cmd_executed=None,
            stdout_tail=f"intent: {action.verb.value} params={action.params}",
        )
        with self._lock:
            self._intents.append(action)
            self._journal.append(result)
        log.info("readonly_log: %s params=%s", action.verb.value, action.params)
        append_journal_event({
            "kind": "apply",
            "actuator": self.name,
            "applied_at": result.applied_at,
            "cmd_executed": None,
            "stdout_tail": result.stdout_tail,
            "error": None,
            "verb": action.verb.value,
            "params": dict(action.params),
            "expires_at": action.expires_at,
        })
        return result

    def revert(self) -> None:
        # readonly — нечего откатывать
        return None

    def journal(self) -> Iterable[ActionResult]:
        with self._lock:
            return list(self._journal)


def make() -> ReadonlyActuator:
    return ReadonlyActuator()
