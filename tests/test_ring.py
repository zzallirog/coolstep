"""Ring buffer tests."""

from __future__ import annotations

import pytest

from coolstep.core.ring import Ring
from coolstep.core.schema import TelemetryFrame


def _frame(ts: float) -> TelemetryFrame:
    return TelemetryFrame(timestamp=ts)


def test_ring_capacity_validation() -> None:
    with pytest.raises(ValueError):
        Ring(capacity=0)


def test_ring_push_and_latest() -> None:
    ring = Ring(capacity=3)
    assert ring.latest() is None
    ring.push(_frame(1.0))
    ring.push(_frame(2.0))
    assert ring.latest() is not None
    assert ring.latest().timestamp == 2.0  # type: ignore[union-attr]


def test_ring_evicts_oldest_at_capacity() -> None:
    ring = Ring(capacity=3)
    for ts in (1.0, 2.0, 3.0, 4.0):
        ring.push(_frame(ts))
    timestamps = [f.timestamp for f in ring]
    assert timestamps == [2.0, 3.0, 4.0]


def test_ring_window_oldest_first() -> None:
    ring = Ring(capacity=10)
    for ts in (1.0, 2.0, 3.0, 4.0):
        ring.push(_frame(ts))
    window = ring.window(2)
    assert [f.timestamp for f in window] == [3.0, 4.0]


def test_ring_window_more_than_size_returns_all() -> None:
    ring = Ring(capacity=10)
    ring.push(_frame(1.0))
    ring.push(_frame(2.0))
    assert [f.timestamp for f in ring.window(50)] == [1.0, 2.0]


def test_ring_window_zero_or_negative() -> None:
    ring = Ring(capacity=10)
    ring.push(_frame(1.0))
    assert ring.window(0) == []
    assert ring.window(-3) == []


def test_ring_since_filters_by_timestamp() -> None:
    ring = Ring(capacity=10)
    for ts in (1.0, 2.0, 3.0, 4.0):
        ring.push(_frame(ts))
    recent = ring.since(3.0)
    assert [f.timestamp for f in recent] == [3.0, 4.0]


def test_ring_len_grows_then_caps() -> None:
    ring = Ring(capacity=3)
    assert len(ring) == 0
    for ts in (1.0, 2.0):
        ring.push(_frame(ts))
    assert len(ring) == 2
    for ts in (3.0, 4.0, 5.0):
        ring.push(_frame(ts))
    assert len(ring) == 3
