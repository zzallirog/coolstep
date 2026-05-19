"""Actuator contract + shared journal-persist helper."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)

# Append-only file of actuator events (apply / revert). Each line is one
# self-contained JSON record. The dashboard tails this file via
# /api/actuator-journal — daemon's in-memory journal is invisible to the
# dashboard process otherwise (separate Python processes).
JOURNAL_FILE = "actuator-journal.jsonl"
# Soft cap: tail N lines on read; older history stays on disk for forensics.
JOURNAL_TAIL_DEFAULT = 200

# Size-based rotation: 3 generations on disk (.jsonl / .jsonl.1 / .jsonl.2).
# Each rename is atomic; the full chain is not — best-effort only.
# Override via env var at process start (cached once at import time).
MAX_JOURNAL_BYTES: int = int(
    os.environ.get("COOLSTEP_JOURNAL_MAX_BYTES", "1000000")
)


def _journal_path() -> Path:
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / JOURNAL_FILE


def _rotate_journal(path: Path) -> None:
    """Rotate journal files: .1→.2, active→.1, fresh active created on next open.

    Best-effort: any OSError → log debug, return without rotating.
    """
    gen2 = Path(str(path) + ".2")
    gen1 = Path(str(path) + ".1")
    try:
        if gen1.exists():
            os.rename(gen1, gen2)
        os.rename(path, gen1)
    except OSError as exc:
        log.debug("journal rotation failed: %r", exc)


def append_journal_event(record: dict) -> None:
    """Best-effort jsonl append with size-based rotation.

    Before appending, checks file size against MAX_JOURNAL_BYTES. If at or
    over threshold, rotates (active→.1, .1→.2). Failures in rotation or
    append are logged at debug and never raised — a failed journal write must
    not break the actuator's hot path.
    """
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_JOURNAL_BYTES:
            _rotate_journal(path)
        record.setdefault("ts", time.time())
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("journal append failed: %r", exc)


@runtime_checkable
class Actuator(Protocol):
    name: str

    def supports(self, verb: ActionVerb) -> bool: ...
    def dry_run(self, action: Action) -> SimResult: ...
    def apply(self, action: Action) -> ActionResult: ...
    def revert(self) -> None: ...
