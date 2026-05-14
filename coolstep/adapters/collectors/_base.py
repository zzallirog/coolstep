"""Abstract collector contract + cost timing helper."""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from coolstep.core.schema import Cost, SignalDescriptor


@runtime_checkable
class Collector(Protocol):
    name: str

    def discover(self) -> bool: ...
    def sample(self) -> dict[str, object]: ...
    def cost(self) -> Cost: ...
    def signals(self) -> list[SignalDescriptor]: ...


class CostTracker:
    """Helper that tracks rolling sample latency.

    Adapters compose this; not a base class.
    """

    def __init__(self, window: int = 32) -> None:
        self._samples_us: list[int] = []
        self._window = window

    def record_us(self, dt_us: int) -> None:
        self._samples_us.append(dt_us)
        if len(self._samples_us) > self._window:
            self._samples_us = self._samples_us[-self._window :]

    def avg_us(self) -> int:
        return int(sum(self._samples_us) / len(self._samples_us)) if self._samples_us else 0


class _StopWatch:
    """Context manager — measures wall time in μs."""

    __slots__ = ("_start", "elapsed_us")

    def __init__(self) -> None:
        self._start = 0
        self.elapsed_us = 0

    def __enter__(self) -> _StopWatch:
        self._start = time.perf_counter_ns()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_us = (time.perf_counter_ns() - self._start) // 1000


def stopwatch() -> _StopWatch:
    return _StopWatch()
