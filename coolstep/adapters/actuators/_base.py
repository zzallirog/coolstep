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


# ── batch-defer (external compute-batch owner) ─────────────────────────────
#
# Symmetric to the game-mode defer: an external owner (the off-hours
# `reading-batch-power` script around `reading-warm-premium.service`) takes
# the wheel on the machine's acoustic profile — it caps CPU freq, locks the
# NVIDIA clock, and wants the box quiet. While it runs, coolstep's CPU-fan
# actuators should stand down (the same "exactly one (or zero) actuator owns
# the verb at any moment" contract as game-mode), so the two stop fighting
# the fan and corrupting coolstep's baseline / efficiency model.
#
# The owner signals "I'm running" by touching a sentinel FILE (cheap, survives
# the batch process, trivially inspectable, removed even on failure via the
# service's ExecStopPost restore). coolstep only READS it — it never writes
# the file and never actuates CPU freq / GPU clock itself (those stay outside
# coolstep's authority per docs/curve-ownership.md).
#
# Default: ON (defer when the flag is present). Kill switch:
# COOLSTEP_BATCH_DEFER=0 disables the probe entirely (coolstep keeps biasing
# even while the batch runs — the cooperative analogue of GAME_MODE_DEFER=0).
DEFAULT_BATCH_DEFER_FILE = "batch-defer.flag"


def _batch_defer_path() -> Path:
    """Sentinel file the external compute-batch owner touches while running.

    Override with COOLSTEP_BATCH_DEFER_PATH (absolute path). Default sits next
    to the journal in COOLSTEP_HOME so it shares the daemon's data dir and the
    same per-host privacy boundary.
    """
    override = os.environ.get("COOLSTEP_BATCH_DEFER_PATH")
    if override:
        return Path(override)
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / DEFAULT_BATCH_DEFER_FILE


def is_batch_defer_active() -> bool:
    """True when an external compute-batch owner has signalled it's running.

    Two conditions, both required:
      * COOLSTEP_BATCH_DEFER is not disabled ("0"/"false") — default-on.
      * The sentinel file exists.

    Any error stat-ing the file is treated as "not active" — a probe failure
    must never block coolstep (mirrors the game-mode probe's fail-open-to-
    actuating posture). No caching here: a single `Path.exists()` is cheaper
    than the monotonic-clock bookkeeping the systemctl probe needs.
    """
    if os.environ.get("COOLSTEP_BATCH_DEFER", "1").lower() in {"0", "false"}:
        return False
    try:
        return _batch_defer_path().exists()
    except OSError as exc:
        log.debug("batch-defer probe failed: %r", exc)
        return False


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
