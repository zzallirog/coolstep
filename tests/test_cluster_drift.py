"""Tests for coolstep.core.cluster_drift.detect_cluster_drift."""

from __future__ import annotations

import pytest

from coolstep.core.cluster_drift import (
    MIN_SAMPLES_PER_BUCKET,
    detect_cluster_drift,
)


class _FakeChroma:
    """Mirrors ChromaStore.list_stable() — returns stable-flagged rows."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)

    def list_stable(self, limit: int = 100_000):  # noqa: ARG002
        return list(self._rows)


def _row(ts: float, cls: str, temp: float) -> dict:
    return {
        "ts": ts,
        "metadata": {
            "is_stable": 1,
            "workload_class": cls,
            "cpu_temp_at": temp,
        },
    }


NOW = 1_000_000.0
DAY = 24 * 3600.0


def test_returns_empty_when_store_missing_method():
    class _Bare: ...
    assert detect_cluster_drift(_Bare(), window_days=7, now=NOW) == {}


def test_returns_empty_when_window_too_small():
    chroma = _FakeChroma([_row(NOW - 100, "code", 60.0)])
    assert detect_cluster_drift(chroma, window_days=1, now=NOW) == {}


def test_drift_positive_when_recent_hotter_than_trailing():
    """5 cool samples yesterday baseline + 5 hot samples today → drift > 0."""
    rows = []
    # trailing: 5 samples ~3-6 days ago at 60°C
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 3 * DAY - i * 60.0, "code", 60.0))
    # recent: 5 samples in the last hour at 68°C (8°C hotter for same scene)
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 60.0 * (i + 1), "code", 68.0))

    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    assert "code" in out
    assert out["code"] == pytest.approx(8.0)


def test_drift_negative_when_recent_cooler():
    """Cleaning the fan: recent samples are 4°C cooler."""
    rows = []
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 3 * DAY - i * 60.0, "render", 72.0))
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 60.0 * (i + 1), "render", 68.0))

    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    assert out["render"] == pytest.approx(-4.0)


def test_skips_class_with_too_few_recent_samples():
    """Only 2 recent samples → below MIN_SAMPLES_PER_BUCKET → no entry."""
    rows = []
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 3 * DAY - i * 60.0, "code", 60.0))
    rows.append(_row(NOW - 60.0, "code", 70.0))
    rows.append(_row(NOW - 120.0, "code", 70.0))
    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    assert "code" not in out


def test_skips_class_with_too_few_trailing_samples():
    """Only 2 trailing samples → skip class."""
    rows = []
    rows.append(_row(NOW - 3 * DAY, "code", 60.0))
    rows.append(_row(NOW - 4 * DAY, "code", 61.0))
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 60.0 * (i + 1), "code", 70.0))
    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    assert "code" not in out


def test_multiple_classes_independent():
    """Different drift values per workload class come out separately."""
    rows = []
    # code: +5°C drift
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 3 * DAY - i * 60.0, "code", 60.0))
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 60.0 * (i + 1), "code", 65.0))
    # render: -2°C drift
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 3 * DAY - i * 60.0, "render", 75.0))
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 60.0 * (i + 1), "render", 73.0))

    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    assert out["code"] == pytest.approx(5.0)
    assert out["render"] == pytest.approx(-2.0)


def test_falls_back_to_workload_label_when_class_missing():
    """If `workload_class` absent, fall back to legacy `workload_label`."""
    rows = []
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append({
            "ts": NOW - 3 * DAY - i * 60.0,
            "metadata": {"is_stable": 1, "workload_label": "code", "cpu_temp_at": 60.0},
        })
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append({
            "ts": NOW - 60.0 * (i + 1),
            "metadata": {"is_stable": 1, "workload_label": "code", "cpu_temp_at": 68.0},
        })
    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    assert out["code"] == pytest.approx(8.0)


def test_handles_list_stable_raising():
    """A noisy store that throws inside list_stable returns {} gracefully."""

    class _Bad:
        def list_stable(self, limit: int = 0):  # noqa: ARG002
            raise RuntimeError("boom")

    assert detect_cluster_drift(_Bad(), window_days=7, now=NOW) == {}


def test_filters_out_rows_outside_window():
    """Rows older than `window_days` or in the future are ignored."""
    rows = []
    # Way too old (8 days back; window=7 means floor at NOW - 7d).
    rows.append(_row(NOW - 8 * DAY, "code", 200.0))
    # Future timestamp.
    rows.append(_row(NOW + DAY, "code", 200.0))
    # Valid trailing baseline.
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 3 * DAY - i * 60.0, "code", 60.0))
    # Valid recent.
    for i in range(MIN_SAMPLES_PER_BUCKET):
        rows.append(_row(NOW - 60.0 * (i + 1), "code", 60.0))
    chroma = _FakeChroma(rows)
    out = detect_cluster_drift(chroma, window_days=7, now=NOW)
    # If the 200°C outliers had leaked in, drift would be huge. They didn't.
    assert out["code"] == pytest.approx(0.0)
