"""Predictor interface + baselines.

Predictor takes a fingerprint dict + recent ring window and emits a Prediction
describing the chance of a thermal/throttle event in the next horizon.

P0 baseline: AlwaysIdleBaseline.
P1.0 KnnPredictor: top-K cosine neighbours over ChromaDB, weighted vote.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from coolstep.core.schema import LABEL_COOL, LABEL_HOT, LABEL_UNKNOWN, TelemetryFrame


@dataclass(slots=True)
class Neighbour:
    ts: float
    distance: float
    was_hot_in_30s: int            # one of LABEL_UNKNOWN / LABEL_COOL / LABEL_HOT
    peak_temp_after: float | None
    workload_label: str | None
    cpu_temp_at: float | None


@dataclass(slots=True)
class Prediction:
    horizon_sec: float
    throttle_prob: float           # 0..1 — probability of any throttle event
    expected_temp_c: float | None  # ML-estimated peak temp in horizon, if model supports
    confidence: float              # 0..1 — model self-reported confidence
    model_name: str
    features_used: list[str]
    neighbours: list[Neighbour] = field(default_factory=list)
    reason: str = ""               # short explanation for dashboard
    # P2.5-E: count of returned hot/cool neighbours that were flagged
    # `was_danger_vector=1` by backfill. >= 3 + current load jump = a
    # "spike trigger" that lets the daemon bypass REARM_GAP_SEC.
    danger_neighbour_count: int = 0
    # P2.5-F: inverse-distance weighted mean equilibrium_rpm across the
    # stable-state KNN query. None if the stable query returned nothing
    # (cold start). Read by curve.rpm_target.
    suggested_rpm: float | None = None


class Predictor(Protocol):
    name: str
    horizon_sec: float

    def predict(
        self,
        features: dict[str, float],
        window: Sequence[TelemetryFrame],
    ) -> Prediction: ...


class AlwaysIdleBaseline:
    """P0 stub: never predicts trouble. Lets the pipeline run end-to-end with
    no actions until a real model is trained in P1."""

    name = "always_idle_baseline"
    horizon_sec = 5.0

    def predict(
        self,
        features: dict[str, float],
        window: Sequence[TelemetryFrame],
    ) -> Prediction:
        return Prediction(
            horizon_sec=self.horizon_sec,
            throttle_prob=0.0,
            expected_temp_c=features.get("cpu_temp_max"),
            confidence=1.0,
            model_name=self.name,
            features_used=sorted(features.keys()),
            reason="placeholder predictor — pipeline shake-out only",
        )


class TrajectoryBaseline:
    """Saturation-aware short-horizon temperature forecast.

    Naive linear extrapolation (`current + slope × horizon`) systematically
    overshoots on ramp because it assumes the instantaneous slope holds
    constant for the entire horizon.  Real silicon under an active fan
    follows Newton's-style cooling toward an equilibrium temperature:

        T(t) = T_eq + (T_0 - T_eq) × exp(-t/τ)
             = T_0 + slope × τ × (1 - exp(-t/τ))

    where τ is the thermal time-constant for the chip + heatsink + active
    fan loop.  Empirically τ ≈ 3-5 s on a TUF A15-class laptop with the
    fan actually responding — short enough that a 5 s horizon already
    captures most of the asymptote, long enough that ramp moments diverge
    meaningfully from plateau moments.

    Used when ChromaDB / KNN_v1 is unavailable (cold start, masked-by-flag
    SEGV).  The trajectory_signal in KnnPredictor remains the authoritative
    spike-trigger path; this baseline fills the expected_temp_c slot for
    visualization and audit.

    Why the constants are what they are:
      τ = 4.0 s    average laptop chip+fan response; sets the curve shape
      max slope    ±3°C/s clamps sensor-flap / hotplug artifacts from
                   painting a 100°C dot off a 0.2 s glitch
      output clamp 20-120°C silicon-plausible envelope
    """

    name = "trajectory_baseline"
    horizon_sec = 5.0
    tau_sec = 4.0
    max_abs_slope = 3.0  # °C/s — clamp sensor artifacts

    def predict(
        self,
        features: dict[str, float],
        window: Sequence[TelemetryFrame],
    ) -> Prediction:
        import math
        current = features.get("cpu_temp_max")
        slope = features.get("cpu_temp_slope_per_sec", 0.0) or 0.0
        # Clamp pathological slopes from sensor flap.
        slope = max(-self.max_abs_slope, min(self.max_abs_slope, slope))
        if current is None:
            expected: float | None = None
        else:
            # Saturation factor: 1 - exp(-h/τ).  At h=τ → 0.632, h=2τ →
            # 0.865, h→∞ → 1.0.  Naive linear would have factor h/τ = 1.25
            # for h=5/τ=4, so the saturation-aware estimate is ~37% smaller
            # — exactly the overshoot magnitude we observed empirically.
            factor = 1.0 - math.exp(-self.horizon_sec / self.tau_sec)
            asymptote_delta = slope * self.tau_sec   # = T_eq - T_0
            expected_raw = current + asymptote_delta * factor
            expected = max(20.0, min(120.0, expected_raw))
        return Prediction(
            horizon_sec=self.horizon_sec,
            throttle_prob=0.0,
            expected_temp_c=expected,
            confidence=0.5,
            model_name=self.name,
            features_used=sorted(features.keys()),
            reason=f"saturation extrapolation: T0 + slope·τ·(1-exp(-h/τ))  [τ={self.tau_sec}s]",
        )


class KnnPredictor:
    """Top-K cosine neighbours over ChromaDB → weighted vote on `was_hot_in_30s`.

    Confidence:
      - 0.0 if vector is None (Embedder not fitted yet) or no neighbours
      - len(labeled_neighbours) / k * agreement_ratio otherwise

    Agreement ratio = max(votes_hot, votes_cool) / total_labeled. Models
    "neighbours strongly disagree → low confidence" naturally.

    Trajectory fallback (parallel signal):
      KNN does well on workloads it has *seen* (Steam games, build storms).
      It fails on novel CPU-only stressors (stress-ng, encoder benchmarks)
      whose fingerprints don't match recorded neighbours — chip can run at
      94°C and KNN still reports "0/20 hot". A physics-first overlay catches
      these: high current temperature + steep slope = imminent throttle
      regardless of recognition. Thresholds tuned around Arrhenius knee at
      ~78-85°C (silicon failure rate doubles per +10°C above ~70°C).

      When trajectory dominates KNN we *bump* confidence (up to 0.7) so the
      RAMP_COOLING action can actually arm — otherwise low KNN confidence
      would gate the safety net out.
    """

    horizon_sec = 30.0
    name = "knn_v1"

    def __init__(self, embedder, store, top_k: int = 20) -> None:  # type: ignore[no-untyped-def]
        self._embedder = embedder
        self._store = store
        self.top_k = top_k

    def _stable_suggested_rpm(self, vector: list[float]) -> float | None:
        """Run a secondary KNN query restricted to `is_stable=1` neighbours
        and return an inverse-distance-weighted mean of their
        `equilibrium_rpm`. None if no stable neighbours are available yet
        (cold start) or the store doesn't expose `query_stable`.

        Inverse-distance weight: w_i = 1 / (distance_i + eps). Cosine
        distances are 0..2 so eps = 1e-3 keeps the weight finite at distance
        0 without dominating moderate-distance neighbours.
        """
        query_stable = getattr(self._store, "query_stable", None)
        if query_stable is None:
            return None
        try:
            results = query_stable(vector, top_k=10)
        except Exception:  # noqa: BLE001
            return None
        if not results:
            return None
        eps = 1.0e-3
        weighted_sum = 0.0
        weight_total = 0.0
        for r in results:
            meta = r.get("metadata") or {}
            rpm_raw = meta.get("equilibrium_rpm")
            try:
                rpm = float(rpm_raw)
            except (TypeError, ValueError):
                continue
            # Backfill writes -1.0 for non-stable; guard against any sentinel
            # that may slip through the where-filter on legacy data.
            if rpm <= 0.0:
                continue
            distance = float(r.get("distance", 0.0))
            w = 1.0 / (max(distance, 0.0) + eps)
            weighted_sum += w * rpm
            weight_total += w
        if weight_total <= 0.0:
            return None
        return weighted_sum / weight_total

    def _trajectory_signal(self, features: dict[str, float]) -> tuple[float, str]:
        """Physics-first overlay: high temp + steep slope → throttle imminent.

        Thresholds (private, intentionally not module-level constants):
        - HOT_NOW_C = 78.0  → Arrhenius knee for 7nm/5nm silicon; above this
                              MTBF starts collapsing measurably
        - FAST_SLOPE = 1.0 °C/s  → 30s headroom from 78°C to TjMax 108°C is
                                    eaten in ~30s; act now
        - MED_SLOPE  = 0.5 °C/s  → still ~60s of runway, but with workload
                                    persisting we'll hit the wall — pre-cool
        - PAST_KNEE_C = 85.0  → already deep in derate territory, no slope
                                 info needed (steady-state hot)
        """
        slope = features.get("cpu_temp_slope_per_sec", 0.0) or 0.0
        cur_max = features.get("cpu_temp_max", 0.0) or 0.0
        HOT_NOW_C = 78.0
        FAST_SLOPE = 1.0
        MED_SLOPE = 0.5
        PAST_KNEE_C = 85.0
        if cur_max >= HOT_NOW_C and slope >= FAST_SLOPE:
            return 0.9, f"trajectory: {cur_max:.1f}°C rising {slope:.2f}°C/s"
        if cur_max >= HOT_NOW_C and slope >= MED_SLOPE:
            return 0.7, f"trajectory: {cur_max:.1f}°C rising {slope:.2f}°C/s"
        if cur_max >= PAST_KNEE_C:
            return 0.65, f"trajectory: {cur_max:.1f}°C (already past knee)"
        return 0.0, ""

    def predict(
        self,
        features: dict[str, float],
        window: Sequence[TelemetryFrame],
    ) -> Prediction:
        traj_prob, traj_reason = self._trajectory_signal(features)
        if not window:
            return _merge_with_trajectory(
                _zero_pred(self.name, "empty window"), traj_prob, traj_reason, features
            )
        latest = window[-1]
        vector = self._embedder.embed(latest)
        if vector is None:
            return _merge_with_trajectory(
                _zero_pred(self.name, f"embedder cold: <{self._embedder.min_frames_to_fit} frames"),
                traj_prob, traj_reason, features,
            )

        raw = self._store.query(vector, top_k=self.top_k)
        if not raw:
            return _merge_with_trajectory(
                _zero_pred(self.name, "no neighbours yet"), traj_prob, traj_reason, features
            )

        neighbours: list[Neighbour] = []
        labeled_hot = 0
        labeled_cool = 0
        danger_neighbour_count = 0
        peak_temps_after: list[float] = []
        for r in raw:
            meta = r.get("metadata") or {}
            label = int(meta.get("was_hot_in_30s", LABEL_UNKNOWN))
            peak_after_meta = meta.get("peak_temp_after")
            peak_after = float(peak_after_meta) if isinstance(peak_after_meta, (int, float)) else None
            cpu_at_meta = meta.get("cpu_temp_at")
            cpu_at = float(cpu_at_meta) if isinstance(cpu_at_meta, (int, float)) else None
            n_label_meta = meta.get("workload_label")
            n_label = str(n_label_meta) if n_label_meta else None
            neighbours.append(
                Neighbour(
                    ts=r["ts"],
                    distance=r["distance"],
                    was_hot_in_30s=label,
                    peak_temp_after=peak_after,
                    workload_label=n_label,
                    cpu_temp_at=cpu_at,
                )
            )
            if label == LABEL_HOT:
                labeled_hot += 1
                if peak_after is not None:
                    peak_temps_after.append(peak_after)
            elif label == LABEL_COOL:
                labeled_cool += 1
            # P2.5-E: count danger flags regardless of label gate — backfill
            # only sets was_danger_vector=1 when was_hot_in_30s=HOT anyway,
            # so this is consistent without double-counting.
            try:
                if int(meta.get("was_danger_vector", 0)) == 1:
                    danger_neighbour_count += 1
            except (TypeError, ValueError):
                pass

        # P2.5-F: secondary stable-state query → suggested_rpm.
        suggested_rpm = self._stable_suggested_rpm(vector)

        labeled = labeled_hot + labeled_cool
        if labeled < max(3, self.top_k // 4):
            knn_pred = Prediction(
                horizon_sec=self.horizon_sec,
                throttle_prob=0.0,
                expected_temp_c=features.get("cpu_temp_max"),
                confidence=0.0,
                model_name=self.name,
                features_used=sorted(features.keys()),
                neighbours=neighbours,
                reason=f"only {labeled} of {len(raw)} neighbours labeled (<lookahead window)",
                danger_neighbour_count=danger_neighbour_count,
                suggested_rpm=suggested_rpm,
            )
            return _merge_with_trajectory(knn_pred, traj_prob, traj_reason, features)

        prob = labeled_hot / labeled
        agreement = max(labeled_hot, labeled_cool) / labeled
        coverage = labeled / self.top_k
        confidence = coverage * agreement
        expected_peak = max(peak_temps_after) if peak_temps_after else features.get("cpu_temp_max")
        knn_pred = Prediction(
            horizon_sec=self.horizon_sec,
            throttle_prob=prob,
            expected_temp_c=expected_peak,
            confidence=confidence,
            model_name=self.name,
            features_used=sorted(features.keys()),
            neighbours=neighbours,
            reason=(
                f"{labeled_hot}/{labeled} neighbours hot in 30s "
                f"(coverage {coverage:.0%}, agreement {agreement:.0%})"
            ),
            danger_neighbour_count=danger_neighbour_count,
            suggested_rpm=suggested_rpm,
        )
        return _merge_with_trajectory(knn_pred, traj_prob, traj_reason, features)


def _merge_with_trajectory(
    knn: Prediction,
    traj_prob: float,
    traj_reason: str,
    features: dict[str, float],
) -> Prediction:
    """Combine KNN result with trajectory overlay.

    Rules:
      - throttle_prob = max(knn.throttle_prob, traj_prob)
      - if trajectory dominates: bump confidence to min(0.7, knn_conf + 0.2)
        (enough to clear typical min_arm_confidence=0.5), and concatenate
        reasons so dashboard shows both signals
      - else: leave KNN fields as-is
      - expected_temp_c and neighbours always come from KNN — trajectory is
        a probability overlay, not a forecaster
    """
    if traj_prob <= knn.throttle_prob:
        return knn
    new_conf = min(0.7, knn.confidence + 0.2)
    merged_reason = f"{knn.reason} | {traj_reason}" if knn.reason else traj_reason
    return Prediction(
        horizon_sec=knn.horizon_sec,
        throttle_prob=traj_prob,
        expected_temp_c=knn.expected_temp_c,
        confidence=new_conf,
        model_name=knn.model_name,
        features_used=knn.features_used,
        neighbours=knn.neighbours,
        reason=merged_reason,
        danger_neighbour_count=knn.danger_neighbour_count,
        suggested_rpm=knn.suggested_rpm,
    )


def _zero_pred(name: str, reason: str) -> Prediction:
    return Prediction(
        horizon_sec=30.0,
        throttle_prob=0.0,
        expected_temp_c=None,
        confidence=0.0,
        model_name=name,
        features_used=[],
        neighbours=[],
        reason=reason,
    )
