"""Drift detector tests."""

from __future__ import annotations

import json
import time

from coolstep.core.drift import append_history, evaluate


def _write_state(path, **fields):
    payload = {
        "tick": 100,
        "ts": time.time(),
        "model_name": "knn_v1",
        "throttle_prob": 0.0,
        "confidence": 0.5,
        "embedder_fitted": True,
        "chroma_count": 1000,
        **fields,
    }
    path.write_text(json.dumps(payload))


def test_evaluate_missing_state_high_severity(tmp_path):
    rep = evaluate(tmp_path / "absent.json", None)
    assert rep.severity == 1.0
    assert rep.indicators[0].name == "ml_state_missing"


def test_evaluate_corrupt_state_high_severity(tmp_path):
    p = tmp_path / "ml.json"
    p.write_text("{not json")
    rep = evaluate(p, None)
    assert rep.severity == 1.0


def test_evaluate_healthy_state_low_severity(tmp_path):
    p = tmp_path / "ml.json"
    _write_state(p)
    rep = evaluate(p, None)
    assert rep.severity == 0.0
    assert rep.indicators == []


def test_evaluate_cold_embedder_flagged(tmp_path):
    p = tmp_path / "ml.json"
    _write_state(p, embedder_fitted=False, chroma_count=10)
    rep = evaluate(p, None)
    names = [i.name for i in rep.indicators]
    assert "embedder_cold" in names


def test_evaluate_low_confidence_with_warm_store(tmp_path):
    p = tmp_path / "ml.json"
    _write_state(p, chroma_count=500, confidence=0.05)
    rep = evaluate(p, None)
    names = [i.name for i in rep.indicators]
    assert "knn_low_confidence" in names


def test_append_history_appends_jsonl(tmp_path):
    p = tmp_path / "history.jsonl"
    snap = {"throttle_prob": 0.1, "confidence": 0.5, "chroma_count": 100}
    append_history(p, snap)
    append_history(p, {**snap, "confidence": 0.6})
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["confidence"] == 0.5
    assert parsed[1]["confidence"] == 0.6


def test_evaluate_confidence_drop_with_history(tmp_path):
    """С history где confidence упало — индикатор срабатывает."""
    history = tmp_path / "h.jsonl"
    state = tmp_path / "ml.json"
    # Baseline: 8 samples with high confidence
    for _ in range(8):
        append_history(history, {"throttle_prob": 0.5, "confidence": 0.8, "chroma_count": 100})
    # Recent: low confidence
    for _ in range(2):
        append_history(history, {"throttle_prob": 0.5, "confidence": 0.2, "chroma_count": 100})
    _write_state(state, confidence=0.2)
    rep = evaluate(state, history)
    names = [i.name for i in rep.indicators]
    assert "confidence_drop" in names


def test_evaluate_snapshot_stale_flagged(tmp_path):
    """ml-state.json старше 90s → snapshot_stale indicator."""
    import os
    p = tmp_path / "ml.json"
    _write_state(p)
    # Push mtime to 5 minutes ago
    old = time.time() - 300
    os.utime(p, (old, old))
    rep = evaluate(p, None)
    names = [i.name for i in rep.indicators]
    assert "snapshot_stale" in names
    stale = next(i for i in rep.indicators if i.name == "snapshot_stale")
    assert stale.severity > 0.4  # ~300/600 = 0.5


def test_evaluate_chroma_no_growth_flagged(tmp_path):
    """Flat chroma_count across history window → chroma_no_growth indicator."""
    history = tmp_path / "h.jsonl"
    state = tmp_path / "ml.json"
    # 8 baseline samples, all with chroma_count = 500
    for _ in range(8):
        append_history(history, {"throttle_prob": 0.0, "confidence": 0.5, "chroma_count": 500})
    # 2 recent samples — same count → growth = 0
    for _ in range(2):
        append_history(history, {"throttle_prob": 0.0, "confidence": 0.5, "chroma_count": 500})
    _write_state(state, chroma_count=500)
    rep = evaluate(state, history)
    names = [i.name for i in rep.indicators]
    assert "chroma_no_growth" in names


def test_evaluate_chroma_disabled_no_false_positive(tmp_path):
    """Chroma fully disabled (count=0 forever) must NOT fire chroma_no_growth."""
    history = tmp_path / "h.jsonl"
    state = tmp_path / "ml.json"
    for _ in range(10):
        append_history(history, {"throttle_prob": 0.0, "confidence": 0.5, "chroma_count": 0})
    _write_state(state, chroma_count=0, model_name="trajectory_baseline+meta")
    rep = evaluate(state, history)
    names = [i.name for i in rep.indicators]
    assert "chroma_no_growth" not in names
