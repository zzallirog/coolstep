"""Event Segmentation — split the telemetry stream into logical sessions.

A *session* is a contiguous run of frames that share the same physical
context: same focused workload, comparable load level, and a recognisable
thermal regime. Within a session, KNN neighbours found in past sessions
are meaningful comparators; *across* a session boundary they are not.

Three boundary rules (any of them flips the session id):

* ``focus_change``   — ``frame.workload.label`` changed from the previous
  frame. The user switched apps — by definition a new logical scene.
* ``load_jump``      — ``cpu_load_max`` jumped by more than
  ``load_jump_threshold_pct`` % between consecutive frames. A fresh
  workload often starts before the focus signal catches up; the load
  signal is the canary.
* ``plateau_collapse`` — once we have observed ``plateau_window`` frames
  whose ``cpu_load_max`` and ``cpu_temp_max`` stayed within tolerance, we
  declare the system to be in a *plateau*: a long, repetitive band of
  similar frames. Inside the plateau we keep emitting the same session
  id (folds many in-tolerance frames into a single logical event). When
  load or temp finally moves out of tolerance, we close the plateau by
  starting a fresh session, tagged ``plateau_collapse`` so the cause is
  legible in chroma metadata.

The class is a pure in-memory state machine — no I/O, no threading. The
returned ``SessionBoundary`` can be attached to chroma row metadata, then
KNN queries filter ``where={"session_id": ...}`` to ensure neighbours
come from comparable scenes.

ID format: ``"<ts_int>-<reason_letter>"`` where ``reason_letter`` is
``f`` (focus_change), ``j`` (load_jump), ``p`` (plateau_collapse), or
``i`` (initial — first frame ever fed).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Final

from coolstep.core.schema import TelemetryFrame

REASON_LETTER: Final[dict[str, str]] = {
    "focus_change": "f",
    "load_jump": "j",
    "plateau_collapse": "p",
    "initial": "i",
}


@dataclass(slots=True, frozen=True)
class SessionBoundary:
    ts: float
    reason: str
    prev_session_id: str | None
    new_session_id: str


def _cpu_load_max(frame: TelemetryFrame) -> float:
    """Same metric the fingerprint extractor uses: per-frame max core load."""
    if not frame.cpu.load_pct:
        return 0.0
    return max(frame.cpu.load_pct)


def _cpu_temp_max(frame: TelemetryFrame) -> float | None:
    """Tctl/tdie preferred; falls back to the hottest reading."""
    temps = frame.cpu.temps_c
    if not temps:
        return None
    primary = temps.get("tctl") or temps.get("tdie")
    if primary is not None:
        return float(primary)
    values = [v for v in temps.values() if v is not None]
    return max(values) if values else None


def _workload_label(frame: TelemetryFrame) -> str | None:
    return frame.workload.label if frame.workload is not None else None


def _session_id(ts: float, reason: str) -> str:
    letter = REASON_LETTER.get(reason, "x")
    return f"{int(ts)}-{letter}"


class EventSegmenter:
    """In-memory session-boundary detector.

    Call :meth:`feed` once per frame. The first call emits an *initial*
    boundary (``prev_session_id is None``); subsequent calls return a
    boundary only when one of the three trigger rules fires. Otherwise
    returns ``None`` — the current session continues.

    Parameters
    ----------
    load_jump_threshold_pct
        ``cpu_load_max`` delta in percentage points that counts as a
        ``load_jump``.
    plateau_window
        Number of trailing frames considered for plateau detection.
    plateau_load_tolerance_pct
        Allowed |Δload| within the plateau window (max minus min).
    plateau_temp_tolerance_c
        Allowed |Δtemp| within the plateau window (max minus min).
    """

    def __init__(
        self,
        *,
        load_jump_threshold_pct: float = 25.0,
        plateau_window: int = 30,
        plateau_load_tolerance_pct: float = 5.0,
        plateau_temp_tolerance_c: float = 1.0,
    ) -> None:
        self._load_jump_threshold = float(load_jump_threshold_pct)
        self._plateau_window = int(plateau_window)
        self._plateau_load_tol = float(plateau_load_tolerance_pct)
        self._plateau_temp_tol = float(plateau_temp_tolerance_c)

        self._current_id: str | None = None
        self._prev_load: float | None = None
        self._prev_label: str | None = None
        self._prev_label_set: bool = False
        # Trailing window of (load, temp). Temp may be None if telemetry
        # didn't provide one — those frames just don't satisfy plateau.
        self._window: deque[tuple[float, float | None]] = deque(
            maxlen=self._plateau_window
        )
        self._in_plateau: bool = False

    # ── public API ──────────────────────────────────────────────────────

    def current_session_id(self) -> str:
        """Return the id of the session in progress.

        Raises ``RuntimeError`` if ``feed()`` has never been called — there
        is no session to identify yet.
        """
        if self._current_id is None:
            raise RuntimeError("EventSegmenter has no session yet; feed() first")
        return self._current_id

    def feed(self, frame: TelemetryFrame) -> SessionBoundary | None:
        """Process one frame; return a SessionBoundary if a new session
        starts, else None."""
        load = _cpu_load_max(frame)
        temp = _cpu_temp_max(frame)
        label = _workload_label(frame)

        # ── first ever frame: emit the initial session ──────────────
        if self._current_id is None:
            boundary = self._start_session(frame.timestamp, reason="initial",
                                           prev_id=None)
            self._record_after(load=load, temp=temp, label=label)
            return boundary

        # ── focus_change ────────────────────────────────────────────
        # Only triggers once we have seen any prior label state. If the
        # previous frame had no workload info and the current does, we
        # treat that as a fresh focus signal (label went from None to X).
        if self._prev_label_set and label != self._prev_label:
            boundary = self._start_session(frame.timestamp, reason="focus_change",
                                           prev_id=self._current_id)
            self._record_after(load=load, temp=temp, label=label)
            return boundary

        # ── load_jump ───────────────────────────────────────────────
        if (
            self._prev_load is not None
            and abs(load - self._prev_load) > self._load_jump_threshold
        ):
            boundary = self._start_session(frame.timestamp, reason="load_jump",
                                           prev_id=self._current_id)
            self._record_after(load=load, temp=temp, label=label)
            return boundary

        # ── plateau bookkeeping ─────────────────────────────────────
        # Tentatively append the new frame's (load, temp) and check
        # whether the trailing window now qualifies as a plateau.
        self._window.append((load, temp))
        was_in_plateau = self._in_plateau
        self._in_plateau = self._window_is_plateau()

        # Plateau just BROKE: previous tick was inside plateau, this one
        # falls outside the tolerance band → spawn a new session.
        if was_in_plateau and not self._in_plateau:
            boundary = self._start_session(frame.timestamp, reason="plateau_collapse",
                                           prev_id=self._current_id)
            self._record_after(load=load, temp=temp, label=label)
            return boundary

        # No trigger — current session continues.
        self._prev_load = load
        self._prev_label = label
        self._prev_label_set = True
        return None

    # ── helpers ─────────────────────────────────────────────────────────

    def _start_session(
        self,
        ts: float,
        *,
        reason: str,
        prev_id: str | None,
    ) -> SessionBoundary:
        new_id = _session_id(ts, reason)
        # Avoid colliding when two boundaries land in the same integer
        # second (synthetic streams in tests, or sub-second tick rates).
        if new_id == prev_id:
            new_id = f"{new_id}+"
        boundary = SessionBoundary(
            ts=float(ts),
            reason=reason,
            prev_session_id=prev_id,
            new_session_id=new_id,
        )
        self._current_id = new_id
        # Reset plateau state — a new session starts with a clean
        # tolerance window.
        self._window.clear()
        self._in_plateau = False
        return boundary

    def _record_after(
        self,
        *,
        load: float,
        temp: float | None,
        label: str | None,
    ) -> None:
        """After emitting a boundary, seed the new session's state with
        the current frame so the next call has something to compare to."""
        self._prev_load = load
        self._prev_label = label
        self._prev_label_set = True
        self._window.append((load, temp))

    def _window_is_plateau(self) -> bool:
        """True iff the trailing window is fully populated and every
        frame's (load, temp) lies inside the tolerance band."""
        if len(self._window) < self._plateau_window:
            return False
        loads = [pair[0] for pair in self._window]
        temps = [pair[1] for pair in self._window if pair[1] is not None]
        if max(loads) - min(loads) > self._plateau_load_tol:
            return False
        # If telemetry didn't ship temps for every frame we can't certify
        # a temperature plateau — refuse to fold.
        if len(temps) != len(self._window):
            return False
        if max(temps) - min(temps) > self._plateau_temp_tol:
            return False
        return True
