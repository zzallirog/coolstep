"""KnnPredictor tests with fake embedder + fake store."""

from __future__ import annotations

import pytest

from coolstep.core.predictor import KnnPredictor
from coolstep.core.schema import LABEL_UNKNOWN, CpuMetrics, TelemetryFrame


class _FakeEmbedder:
    def __init__(self, fitted: bool = True, vector: list[float] | None = None) -> None:
        self.fitted = fitted
        self.min_frames_to_fit = 60
        self._vec = vector if vector is not None else [0.1] * 26

    def embed(self, frame):  # type: ignore[no-untyped-def]
        return self._vec if self.fitted else None


class _FakeStore:
    """In-memory stand-in for chroma.ChromaStore.

    Mirrors the labeled_only filter behaviour of the production store so
    tests exercise predictor logic against realistic neighbour sets. Без
    parity тесты могли «видеть» unlabeled vectors, которые prod ChromaStore
    отфильтровал бы в where-clause до того, как они дойдут до predictor.

    P2.5: optional `stable_results` slot drives `query_stable()` — leaving it
    empty makes the predictor's `suggested_rpm` be None (cold-start path).
    """

    def __init__(
        self,
        results: list[dict],
        stable_results: list[dict] | None = None,
    ) -> None:
        self.results = results
        self.stable_results = stable_results or []

    def query(self, vector, top_k, labeled_only=True):  # type: ignore[no-untyped-def]
        items = self.results
        if labeled_only:
            items = [
                r for r in items
                if (r.get("metadata") or {}).get("was_hot_in_30s") != LABEL_UNKNOWN
            ]
        return items[:top_k]

    def query_stable(self, vector, top_k=10):  # type: ignore[no-untyped-def]
        # Mirror chroma where={"is_stable":1}: only neighbours with
        # is_stable=1 are surfaced. Distance ordering is preserved as given.
        return [
            r for r in self.stable_results
            if (r.get("metadata") or {}).get("is_stable") == 1
        ][:top_k]


def _frame(ts: float, tctl: float = 70.0) -> TelemetryFrame:
    return TelemetryFrame(timestamp=ts, cpu=CpuMetrics(temps_c={"tctl": tctl}))


def test_zero_when_embedder_cold():
    p = KnnPredictor(_FakeEmbedder(fitted=False), _FakeStore([]))
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(1.0)])
    assert pred.throttle_prob == 0.0
    assert pred.confidence == 0.0
    assert "cold" in pred.reason


def test_zero_when_no_neighbours():
    p = KnnPredictor(_FakeEmbedder(), _FakeStore([]))
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(1.0)])
    assert pred.throttle_prob == 0.0
    assert "no neighbours" in pred.reason


def test_majority_hot_high_prob():
    results = [
        {"ts": 1.0, "distance": 0.05, "metadata": {"was_hot_in_30s": 1, "peak_temp_after": 92.0,
                                                    "workload_label": "build", "cpu_temp_at": 80}},
        {"ts": 2.0, "distance": 0.06, "metadata": {"was_hot_in_30s": 1, "peak_temp_after": 91.0,
                                                    "workload_label": "build", "cpu_temp_at": 78}},
        {"ts": 3.0, "distance": 0.07, "metadata": {"was_hot_in_30s": 1, "peak_temp_after": 93.0,
                                                    "workload_label": "build", "cpu_temp_at": 81}},
        {"ts": 4.0, "distance": 0.08, "metadata": {"was_hot_in_30s": 0, "peak_temp_after": 75.0,
                                                    "workload_label": "idle", "cpu_temp_at": 60}},
    ]
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(results), top_k=4)
    pred = p.predict({"cpu_temp_max": 78.0}, [_frame(100.0)])
    assert pred.throttle_prob == 0.75
    assert pred.confidence > 0.5
    assert pred.expected_temp_c == 93.0
    assert len(pred.neighbours) == 4


def test_unknown_labels_drop_confidence():
    """Если большинство соседей с label=-1, confidence близко к нулю."""
    results = [
        {"ts": float(i), "distance": 0.1, "metadata": {"was_hot_in_30s": -1}}
        for i in range(20)
    ]
    results[0]["metadata"]["was_hot_in_30s"] = 1
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(results), top_k=20)
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(100.0)])
    # 1 labeled out of 20; below threshold of max(3, 5)
    assert pred.confidence == 0.0
    assert "labeled" in pred.reason


def test_majority_cool_low_prob():
    results = [
        {"ts": float(i), "distance": 0.1,
         "metadata": {"was_hot_in_30s": 0, "peak_temp_after": 65.0,
                      "workload_label": "idle", "cpu_temp_at": 55}}
        for i in range(10)
    ]
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(results), top_k=10)
    pred = p.predict({"cpu_temp_max": 50.0}, [_frame(100.0)])
    assert pred.throttle_prob == 0.0
    assert pred.confidence > 0.5
    assert "0/10 neighbours hot" in pred.reason


def test_below_min_labeled_threshold_returns_zero_confidence():
    """With top_k=20, min labeled = max(3, 5) = 5. With 4 labeled neighbors
    (below threshold) predictor must return throttle_prob=0, confidence=0
    and reason mentioning the labeled-count gap (predictor-audit risk #5)."""
    labelled = [
        {"ts": float(i), "distance": 0.05, "metadata": {"was_hot_in_30s": 1,
            "peak_temp_after": 92.0, "workload_label": "build", "cpu_temp_at": 88}}
        for i in range(4)
    ]
    unknown = [
        {"ts": float(100 + i), "distance": 0.2, "metadata": {"was_hot_in_30s": -1,
            "peak_temp_after": -1, "workload_label": "build", "cpu_temp_at": 70}}
        for i in range(16)
    ]
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(labelled + unknown), top_k=20)
    pred = p.predict({"cpu_temp_max": 78.0}, [_frame(200.0)])
    assert pred.throttle_prob == 0.0
    assert pred.confidence == 0.0
    # Reason should mention labeled count being insufficient.
    assert "labeled" in pred.reason.lower() or "labelled" in pred.reason.lower()


def test_neighbours_carry_metadata_through():
    results = [
        {"ts": 1.0, "distance": 0.1, "metadata": {"was_hot_in_30s": 1, "peak_temp_after": 92.0,
                                                    "workload_label": "game", "cpu_temp_at": 88}},
    ] + [
        {"ts": float(i), "distance": 0.2, "metadata": {"was_hot_in_30s": 0, "peak_temp_after": 65.0,
                                                        "workload_label": "idle", "cpu_temp_at": 55}}
        for i in range(2, 6)
    ]
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(results), top_k=5)
    pred = p.predict({"cpu_temp_max": 78.0}, [_frame(100.0)])
    assert pred.neighbours[0].workload_label == "game"
    assert pred.neighbours[0].was_hot_in_30s == 1


# ---------------------------------------------------------------------------
# Trajectory fallback signal (physics-first overlay)
# ---------------------------------------------------------------------------

def _cool_results(n: int = 10) -> list[dict]:
    """KNN with majority-cool labels → low throttle_prob (0.0)."""
    return [
        {"ts": float(i), "distance": 0.1,
         "metadata": {"was_hot_in_30s": 0, "peak_temp_after": 65.0,
                      "workload_label": "idle", "cpu_temp_at": 55}}
        for i in range(n)
    ]


def _hot_results(n: int = 10) -> list[dict]:
    """KNN with majority-hot labels → high throttle_prob."""
    return [
        {"ts": float(i), "distance": 0.05,
         "metadata": {"was_hot_in_30s": 1, "peak_temp_after": 92.0,
                      "workload_label": "build", "cpu_temp_at": 88}}
        for i in range(n)
    ]


def test_trajectory_does_not_fire_on_cool_chip():
    """Cool chip + slow slope → trajectory silent, KNN result preserved."""
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_cool_results(10)), top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 50.0, "cpu_temp_slope_per_sec": 0.3},
        [_frame(100.0)],
    )
    assert pred.throttle_prob == 0.0
    assert "trajectory" not in pred.reason


def test_trajectory_overrides_knn_when_hot_and_climbing_fast():
    """Hot + fast slope + KNN says cool → trajectory overrides to 0.9.

    This is the stress-ng / novel-workload scenario: KNN sees no neighbours
    in 30s window so prob ≈ 0, but chip is at 82°C rising 1.5°C/s.
    """
    # KNN sees only cool neighbours → its prob = 0
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_cool_results(10)), top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 82.0, "cpu_temp_slope_per_sec": 1.5},
        [_frame(100.0)],
    )
    assert pred.throttle_prob == 0.9
    assert "trajectory: 82.0" in pred.reason


def test_trajectory_overrides_at_med_slope():
    """Med slope (>= 0.5) + temp >= 78 → trajectory_prob = 0.7."""
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_cool_results(10)), top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 80.0, "cpu_temp_slope_per_sec": 0.6},
        [_frame(100.0)],
    )
    assert pred.throttle_prob == 0.7
    assert "trajectory:" in pred.reason


def test_trajectory_already_past_knee_no_slope():
    """Steady-state hot (88°C, slope=0) → trajectory_prob >= 0.65."""
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_cool_results(10)), top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 88.0, "cpu_temp_slope_per_sec": 0.0},
        [_frame(100.0)],
    )
    assert pred.throttle_prob >= 0.65
    assert "past knee" in pred.reason


def test_knn_dominates_when_higher():
    """KNN says 1.0 prob, trajectory says 0.7 → final keeps KNN's 1.0."""
    # 10 hot neighbours → KNN throttle_prob = 1.0, agreement = 1.0,
    # coverage = 1.0 → confidence = 1.0
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_hot_results(10)), top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 80.0, "cpu_temp_slope_per_sec": 0.6},  # would give 0.7 traj
        [_frame(100.0)],
    )
    assert pred.throttle_prob == 1.0
    assert pred.confidence == 1.0  # unchanged from KNN
    # Reason is pure KNN — no trajectory suffix when KNN dominates
    assert "trajectory" not in pred.reason


def test_trajectory_bumps_confidence_when_dominating():
    """Low-confidence KNN + dominant trajectory → confidence bumps ≥ 0.4.

    Spec: confidence = min(0.7, knn_conf + 0.2). With knn_conf=0.2 →
    final = min(0.7, 0.4) = 0.4. Asserts >= 0.4 per task spec.
    """
    # Build a KNN scenario where labeled count passes threshold but
    # confidence stays low. Top_k=20, labeled=5 (just meets max(3,5)),
    # all cool → agreement=1.0, coverage=5/20=0.25 → confidence=0.25.
    cool5 = [
        {"ts": float(i), "distance": 0.1,
         "metadata": {"was_hot_in_30s": 0, "peak_temp_after": 65.0,
                      "workload_label": "idle", "cpu_temp_at": 55}}
        for i in range(5)
    ]
    unknown15 = [
        {"ts": float(100 + i), "distance": 0.2,
         "metadata": {"was_hot_in_30s": -1}}
        for i in range(15)
    ]
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(cool5 + unknown15), top_k=20)
    pred = p.predict(
        {"cpu_temp_max": 82.0, "cpu_temp_slope_per_sec": 1.5},
        [_frame(100.0)],
    )
    # KNN confidence ~0.25 (5/20 coverage × 1.0 agreement); trajectory
    # dominates → new conf = min(0.7, 0.25+0.2) = 0.45
    assert pred.throttle_prob == 0.9
    assert pred.confidence >= 0.4


def test_reason_contains_both_signals_when_trajectory_dominates():
    """When trajectory wins, reason concatenates KNN reason + trajectory."""
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_cool_results(10)), top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 82.0, "cpu_temp_slope_per_sec": 1.5},
        [_frame(100.0)],
    )
    # KNN's reason about "0/10 neighbours hot" stays, separated by "|"
    assert "neighbours hot" in pred.reason
    assert "trajectory:" in pred.reason
    assert "|" in pred.reason


# ---------------------------------------------------------------------------
# P2.5-E / P2.5-F — Prediction fields (danger_neighbour_count, suggested_rpm)
# ---------------------------------------------------------------------------

def test_prediction_has_p25_fields_by_default():
    """Empty store path still surfaces the P2.5 fields with safe defaults."""
    p = KnnPredictor(_FakeEmbedder(), _FakeStore([]))
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(1.0)])
    # New attributes exist and carry safe defaults.
    assert hasattr(pred, "danger_neighbour_count")
    assert hasattr(pred, "suggested_rpm")
    assert pred.danger_neighbour_count == 0
    assert pred.suggested_rpm is None


def test_danger_neighbour_count_aggregates_from_metadata():
    """Three of five hot neighbours flagged was_danger_vector → count = 3."""
    results = []
    for i in range(5):
        results.append({
            "ts": float(i),
            "distance": 0.05 + 0.01 * i,
            "metadata": {
                "was_hot_in_30s": 1,
                "peak_temp_after": 92.0,
                "workload_label": "build",
                "cpu_temp_at": 86,
                "was_danger_vector": 1 if i < 3 else 0,
            },
        })
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(results), top_k=5)
    pred = p.predict({"cpu_temp_max": 80.0}, [_frame(100.0)])
    assert pred.danger_neighbour_count == 3


def test_suggested_rpm_weighted_mean_inverse_distance():
    """Two stable neighbours: closer one dominates the weighted mean."""
    stable = [
        {"ts": 10.0, "distance": 0.05,
         "metadata": {"is_stable": 1, "equilibrium_rpm": 3000.0}},
        {"ts": 11.0, "distance": 0.5,
         "metadata": {"is_stable": 1, "equilibrium_rpm": 5000.0}},
    ]
    # Hot/cool query stays cool to keep things simple.
    store = _FakeStore(_cool_results(10), stable_results=stable)
    p = KnnPredictor(_FakeEmbedder(), store, top_k=10)
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(100.0)])
    # w1 = 1/(0.05+1e-3) ≈ 19.6, w2 = 1/(0.5+1e-3) ≈ 2.0.
    # weighted_mean = (19.6*3000 + 2.0*5000) / (19.6 + 2.0) ≈ 3185.2
    assert pred.suggested_rpm is not None
    assert 2900.0 < pred.suggested_rpm < 3500.0


def test_suggested_rpm_none_when_no_stable_neighbours():
    """No stable neighbours surfaced → suggested_rpm stays None."""
    p = KnnPredictor(_FakeEmbedder(), _FakeStore(_cool_results(10)), top_k=10)
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(100.0)])
    assert pred.suggested_rpm is None


def test_suggested_rpm_ignores_sentinel_negative_eq_rpm():
    """A stable=1 neighbour whose equilibrium_rpm slipped through as -1
    must be silently filtered out of the weighted mean (defensive)."""
    stable = [
        {"ts": 10.0, "distance": 0.05,
         "metadata": {"is_stable": 1, "equilibrium_rpm": -1.0}},
    ]
    store = _FakeStore(_cool_results(10), stable_results=stable)
    p = KnnPredictor(_FakeEmbedder(), store, top_k=10)
    pred = p.predict({"cpu_temp_max": 70.0}, [_frame(100.0)])
    assert pred.suggested_rpm is None


def test_p25_fields_survive_trajectory_override():
    """When trajectory dominates KNN, the new fields must still propagate."""
    stable = [
        {"ts": 10.0, "distance": 0.1,
         "metadata": {"is_stable": 1, "equilibrium_rpm": 3200.0}},
    ]
    # Mix in a danger-flagged hot neighbour so count > 0.
    base_cool = _cool_results(9)
    danger_hot = {
        "ts": 99.0, "distance": 0.08,
        "metadata": {"was_hot_in_30s": 1, "peak_temp_after": 91.0,
                     "workload_label": "build", "cpu_temp_at": 85,
                     "was_danger_vector": 1},
    }
    store = _FakeStore([danger_hot] + base_cool, stable_results=stable)
    p = KnnPredictor(_FakeEmbedder(), store, top_k=10)
    pred = p.predict(
        {"cpu_temp_max": 82.0, "cpu_temp_slope_per_sec": 1.5},
        [_frame(100.0)],
    )
    # Trajectory overrides the KNN throttle_prob → 0.9
    assert pred.throttle_prob == 0.9
    # …but P2.5 fields survive the rebuild in _merge_with_trajectory
    assert pred.danger_neighbour_count == 1
    assert pred.suggested_rpm == pytest.approx(3200.0, abs=10.0)
