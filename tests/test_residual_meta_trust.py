"""Tests for TrustMode classifier and correct_with_trust (P2.9.7)."""

from __future__ import annotations

from coolstep.core.residual_meta import (
    PRIOR_K,
    ResidualBank,
    TrustMode,
    classify_trust,
)


def test_classify_trust_prior():
    assert classify_trust(0) == TrustMode.PRIOR


def test_classify_trust_shrunk():
    # PRIOR_K == 5; any 0 < n < 5 is SHRUNK
    assert classify_trust(3) == TrustMode.SHRUNK
    assert int(PRIOR_K) == 5, "test assumes PRIOR_K=5"


def test_classify_trust_confident():
    assert classify_trust(10) == TrustMode.CONFIDENT


def test_correct_with_trust_empty_bank_returns_prior():
    bank = ResidualBank()
    features: dict[str, float] = {}
    correction, std, n, mode = bank.correct_with_trust(features)
    assert mode == TrustMode.PRIOR
    assert correction == 0.0
    assert n == 0
