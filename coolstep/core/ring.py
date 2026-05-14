"""In-memory rolling buffer for the most recent telemetry frames.

Used by predictor (rolling-window features) and dashboard (live tail).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from threading import RLock

from coolstep.core.schema import TelemetryFrame


class Ring:
    """Bounded thread-safe deque of TelemetryFrame.

    Capacity is in *frames*, not seconds — at 1 Hz nominal the two coincide.
    """

    def __init__(self, capacity: int = 600) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._buf: deque[TelemetryFrame] = deque(maxlen=capacity)
        self._lock = RLock()

    @property
    def capacity(self) -> int:
        return self._buf.maxlen or 0

    def push(self, frame: TelemetryFrame) -> None:
        with self._lock:
            self._buf.append(frame)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def latest(self) -> TelemetryFrame | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def window(self, n: int) -> list[TelemetryFrame]:
        """Last *n* frames, oldest first. Returns fewer if not enough collected."""
        if n <= 0:
            return []
        with self._lock:
            if n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def since(self, ts: float) -> list[TelemetryFrame]:
        with self._lock:
            return [f for f in self._buf if f.timestamp >= ts]

    def __iter__(self) -> Iterator[TelemetryFrame]:
        with self._lock:
            return iter(list(self._buf))
