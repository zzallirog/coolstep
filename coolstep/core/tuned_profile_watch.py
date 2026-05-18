"""Tuned-profile watcher — explicit signal for "thermal regime changed".

`/etc/tuned/active_profile` is a single-line file written by `tuned-adm`
on every `profile <name>` call.  Watching it gives us a free, explicit
hook for the moment the user (or a script) flips the thermal envelope —
exactly the moment the meta-predictor's prior learning becomes biased.

This module deliberately does no D-Bus, no inotify, no extra daemon: just
a stat + tiny read every 30 ticks (~30 s on the default period).  Cost:
under 100 µs per poll; safe on every Linux + safe to skip when the file
doesn't exist (non-tuned hosts) — `current_profile()` simply returns None
and downstream code keeps using the residual-CUSUM safety net.

Public surface:

    current_profile() -> str | None     # read the live file
    ProfileWatcher                      # stateful: emits transitions
        .poll() -> str | None           # returns new profile if changed
        .last_seen
        .last_changed_ts

See ADR-019 (explicit > implicit, file-watch + CUSUM fallback).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

TUNED_ACTIVE_FILE = Path("/etc/tuned/active_profile")


def current_profile(path: Path = TUNED_ACTIVE_FILE) -> str | None:
    """Read the active tuned profile name, or None if the file is missing
    or unreadable.  The file is one line, sometimes with a trailing newline.

    A profile name of empty string is treated as None — `tuned-adm off` can
    leave the file empty rather than removing it, and "no profile" is the
    same semantic state as "no file."
    """
    try:
        with open(path, encoding="utf-8") as f:
            name = f.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return name or None


@dataclass
class ProfileWatcher:
    """Stateful profile-change detector.

    Stores the last seen profile name and the wall-clock time of the
    last change.  `poll()` is the only mutator: it returns the new
    profile name iff this poll observed a change (i.e. the value differs
    from `last_seen`), else None.

    The first call always returns the current profile, with
    `last_changed_ts` set to the current time — that's the canonical
    "I just woke up, here's what's live" event.  Downstream code can
    distinguish "real flip" from "first poll" via `is_first_poll`."""

    path: Path = TUNED_ACTIVE_FILE
    last_seen: str | None = None
    last_changed_ts: float = 0.0
    is_first_poll: bool = field(default=True)

    def poll(self, now: float | None = None) -> str | None:
        """Return the new profile if it changed (or this is the first
        poll), else None.  `now` defaults to time.time(); accepts an
        override for tests."""
        t = now if now is not None else time.time()
        observed = current_profile(self.path)
        if self.is_first_poll:
            self.last_seen = observed
            self.last_changed_ts = t
            self.is_first_poll = False
            return observed
        if observed != self.last_seen:
            self.last_seen = observed
            self.last_changed_ts = t
            return observed
        return None


__all__ = ["TUNED_ACTIVE_FILE", "current_profile", "ProfileWatcher"]
