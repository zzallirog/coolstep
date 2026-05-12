"""MetaPredictor — composes a base forecast with learned residual correction.

Architecture per ADR-017:

    MetaPredictor.predict(features, window)
      = base.predict(features, window)              ← L1 (TrajectoryBaseline)
      + bank.correct(features).mean                 ← L2 (ResidualBank)
      (confidence narrows when bank.std grows)
      (CUSUM trip → bank.decay → confidence drop)   ← L3 (Cusum)

The base predictor can be any object implementing `.predict(features,
window) -> Prediction`.  In production it's TrajectoryBaseline (Newton
cooling saturation); when ChromaDB is rewired it can be KnnPredictor
without touching this module.

What does NOT live here:
 - residual log writing — that's daemon._validate_pending_predictions.
   This module only reads bank state.
 - profile-change handling — daemon polls ProfileWatcher and calls
   bank.decay_all + cusum.reset on flip.  Keeping triggers in the
   daemon means MetaPredictor stays pure (features → Prediction).
 - on-disk persistence — bank is rebuilt from ResidualLog at daemon
   startup via `ResidualBank.from_log(log)`.

See `tests/test_predictor_meta.py` for the contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from coolstep.core.predictor import Prediction
from coolstep.core.residual_meta import (
    ResidualBank,
    bucket_of,
    compose_confidence,
)
from coolstep.core.schema import TelemetryFrame


class _BasePredictor(Protocol):
    """Structural typing for any predictor MetaPredictor wraps."""

    name: str
    horizon_sec: float

    def predict(
        self,
        features: dict[str, float],
        window: Sequence[TelemetryFrame],
    ) -> Prediction: ...


class MetaPredictor:
    """L1 + L2 composition.  Drop-in replacement for any base predictor.

    The model_name on the emitted Prediction is `f"{base.name}+meta"` so
    audit logs and the dashboard can tell at a glance which path is
    active without losing the base's identity.
    """

    def __init__(
        self,
        base: _BasePredictor,
        bank: ResidualBank | None = None,
        *,
        # Width (in °C) above which std reduces emitted confidence.
        # 5°C ≈ "predictor knows it's been ±5°C off recently here" — that's
        # the band the user starts caring about.
        std_warn_c: float = 5.0,
    ) -> None:
        self.base = base
        self.bank = bank if bank is not None else ResidualBank()
        self.std_warn_c = std_warn_c
        # Mirror the base's name + horizon so daemon code that reads
        # `predictor.horizon_sec` keeps working unchanged.
        self.name = f"{base.name}+meta"
        self.horizon_sec = base.horizon_sec

    def predict(
        self,
        features: dict[str, float],
        window: Sequence[TelemetryFrame],
    ) -> Prediction:
        base_pred = self.base.predict(features, window)
        if base_pred.expected_temp_c is None:
            # Base couldn't predict (no temp data); nothing to correct.
            return base_pred

        correction, std, n_samples = self.bank.correct(features)
        corrected = base_pred.expected_temp_c + correction
        # Same display clamp as TrajectoryBaseline — silicon envelope.
        corrected = max(20.0, min(120.0, corrected))

        # Confidence composition: base_conf × bucket_certainty.
        # bucket_certainty drops from 1.0 toward 0.5 as std grows past
        # std_warn_c.  Warming up (<5 samples) → 0.4 to *reduce*
        # composed confidence below base — honest "I'm learning."
        if n_samples < 5:
            bucket_certainty = 0.4
        else:
            bucket_certainty = max(
                0.5,
                1.0 - min(1.0, std / max(1e-6, 2.0 * self.std_warn_c)),
            )
        composed_conf = compose_confidence(base_pred.confidence, bucket_certainty)

        key = bucket_of(features)
        return Prediction(
            horizon_sec=base_pred.horizon_sec,
            throttle_prob=base_pred.throttle_prob,
            expected_temp_c=corrected,
            confidence=composed_conf,
            model_name=self.name,
            features_used=base_pred.features_used,
            reason=(
                f"{base_pred.reason} | meta-correction {correction:+.2f}°C "
                f"(σ={std:.2f}, n={n_samples}, bucket={key})"
            ),
        )

    def bucket(self, features: dict[str, float]) -> tuple[int, int, int, int]:
        """Expose bucket-of-features so the daemon can stamp it onto the
        ResidualRecord at validation time."""
        return bucket_of(features)


__all__ = ["MetaPredictor"]
