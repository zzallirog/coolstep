"""Incident logger + multi-angle similarity tests — P2.4."""

from __future__ import annotations

import pytest

from coolstep.core.incidents import (
    Incident,
    angle_feature_vector,
    angle_peak_signature,
    angle_predictor_state,
    angle_recent_lineage,
    angle_workload_class,
    find_similar,
    log_incident,
    read_incidents,
)


@pytest.fixture(autouse=True)
def _no_pollution(monkeypatch, tmp_path):
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_INCIDENTS_LOG_DISABLED", raising=False)


def _make(*, ts: float, kind="quiet_eject", peak=83.0, dur=10.0,
          workload="steam", procs=None, prob=0.18, conf=0.9,
          exp=78.0, emb=None) -> Incident:
    return Incident(
        ts=ts,
        kind=kind,
        peak_temp_c=peak,
        duration_s=dur,
        workload_class=workload,
        top_processes=list(procs or ["steam", "csgo_linux64", "kitty"]),
        predictor_state={"throttle_prob": prob, "confidence": conf, "expected_temp_c": exp},
        features={"cpu_temp_max": peak, "cpu_load_max": 80.0},
        mode_at_incident="quiet",
        armed_verbs_at_incident=["reduce_noise"],
        embedding=emb if emb is not None else [0.0] * 26,
    )


# ── log + read round-trip ──────────────────────────────────────────────


def test_log_and_read_round_trip(tmp_path):
    path = tmp_path / "incidents.jsonl"
    log_incident(_make(ts=100.0, peak=82.5), path=path)
    log_incident(_make(ts=200.0, peak=84.0), path=path)
    rows = read_incidents(path=path)
    assert len(rows) == 2
    # newest first
    assert rows[0]["ts"] == 200.0
    assert rows[1]["ts"] == 100.0


def test_read_filters_by_since(tmp_path):
    path = tmp_path / "incidents.jsonl"
    log_incident(_make(ts=100.0), path=path)
    log_incident(_make(ts=200.0), path=path)
    rows = read_incidents(path=path, since_sec=150.0)
    assert [r["ts"] for r in rows] == [200.0]


def test_log_respects_disable_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLSTEP_INCIDENTS_LOG_DISABLED", "1")
    path = tmp_path / "incidents.jsonl"
    log_incident(_make(ts=100.0), path=path)
    assert not path.exists()


# ── angle: feature_vector ─────────────────────────────────────────────


def test_feature_vector_returns_empty_when_no_embedding():
    current = _make(ts=100.0, emb=None).to_dict()
    current["embedding"] = None
    matches = angle_feature_vector(current, [])
    assert matches == []


def test_feature_vector_orders_by_cosine_distance():
    v_self = [1.0] + [0.0] * 25
    v_close = [0.99] + [0.0] * 24 + [0.141]   # cos ≈ 0.99
    v_far = [0.0] * 25 + [1.0]                 # cos = 0
    cur = _make(ts=200.0, emb=v_self).to_dict()
    prior_rows = [
        _make(ts=100.0, emb=v_close).to_dict(),
        _make(ts=90.0, emb=v_far).to_dict(),
    ]
    matches = angle_feature_vector(cur, prior_rows, top_k=2)
    assert len(matches) == 2
    assert matches[0]["ts"] == 100.0   # close one first
    assert matches[1]["ts"] == 90.0
    assert all(m["angle"] == "feature_vector" for m in matches)


# ── angle: workload_class ─────────────────────────────────────────────


def test_workload_class_same_class_outranks_disjoint():
    cur = _make(ts=200.0, workload="steam", procs=["steam", "csgo_linux64"]).to_dict()
    prior_rows = [
        # same class, different procs → bonus + 0 Jaccard for procs
        _make(ts=100.0, workload="steam", procs=["steam_runtime", "wine"]).to_dict(),
        # different class, no overlap
        _make(ts=90.0, workload="cargo", procs=["rustc", "cc1"]).to_dict(),
    ]
    matches = angle_workload_class(cur, prior_rows, top_k=2)
    assert matches[0]["ts"] == 100.0
    assert "workload_class=steam match" in matches[0]["why"]


def test_workload_class_filters_zero_score():
    cur = _make(ts=200.0, workload=None, procs=["unique_proc"]).to_dict()
    prior_rows = [
        _make(ts=100.0, workload=None, procs=["other"]).to_dict(),
    ]
    # No class match + no procs overlap → score 0 → excluded
    matches = angle_workload_class(cur, prior_rows)
    assert matches == []


# ── angle: predictor_state ────────────────────────────────────────────


def test_predictor_state_orders_by_l2():
    cur = _make(ts=200.0, prob=0.2, conf=0.9, exp=78.0).to_dict()
    prior_rows = [
        _make(ts=100.0, prob=0.22, conf=0.88, exp=79.0).to_dict(),  # close
        _make(ts=90.0, prob=0.9, conf=0.4, exp=88.0).to_dict(),      # far
    ]
    matches = angle_predictor_state(cur, prior_rows, top_k=2)
    assert matches[0]["ts"] == 100.0
    assert matches[1]["ts"] == 90.0


# ── angle: peak_signature ─────────────────────────────────────────────


def test_peak_signature_uses_temp_plus_duration_distance():
    cur = _make(ts=200.0, peak=83.0, dur=10.0).to_dict()
    prior_rows = [
        _make(ts=100.0, peak=83.5, dur=10.5).to_dict(),  # |Δ| = 1.0
        _make(ts=90.0,  peak=95.0, dur=30.0).to_dict(),  # |Δ| = 32.0
    ]
    matches = angle_peak_signature(cur, prior_rows, top_k=2)
    assert matches[0]["ts"] == 100.0
    assert matches[0]["distance"] < matches[1]["distance"]


# ── angle: recent_lineage ─────────────────────────────────────────────


def test_recent_lineage_prefers_last_24h():
    now = 1_700_000_000.0
    cur = _make(ts=now).to_dict()
    prior_rows = [
        _make(ts=now - 3600).to_dict(),       # 1h ago — last 24h
        _make(ts=now - 30 * 86400).to_dict(), # 30 days ago — neither
    ]
    matches = angle_recent_lineage(cur, prior_rows, top_k=2)
    assert matches[0]["ts"] == now - 3600
    assert "in the last 24 h" in matches[0]["why"]


def test_recent_lineage_finds_same_weekday_hour():
    now = 1_700_000_000.0
    # 7 days earlier, same hour-of-day & weekday
    prior_ts = now - 7 * 86400
    cur = _make(ts=now).to_dict()
    prior_rows = [
        _make(ts=prior_ts).to_dict(),
        _make(ts=now - 60 * 86400).to_dict(),
    ]
    matches = angle_recent_lineage(cur, prior_rows, top_k=2)
    # Top hit: same weekday+hour, which is the 7d-old row
    assert matches[0]["ts"] == prior_ts
    assert "same weekday" in matches[0]["why"]


# ── find_similar (composite) ──────────────────────────────────────────


def test_find_similar_returns_flat_multi_angle_list(tmp_path):
    path = tmp_path / "incidents.jsonl"
    # Seed three prior incidents
    log_incident(_make(ts=100.0, peak=82.0, emb=[1.0] + [0.0] * 25), path=path)
    log_incident(_make(ts=150.0, peak=84.0, workload="cargo",
                       procs=["rustc"], emb=[0.0] * 25 + [1.0]), path=path)
    log_incident(_make(ts=180.0, peak=83.5, emb=[0.99] + [0.0] * 25), path=path)
    new = _make(ts=200.0, peak=83.0, emb=[1.0] + [0.0] * 25).to_dict()
    matches = find_similar(new, path=path, top_k_per_angle=2)
    angles_returned = set(m["angle"] for m in matches)
    # All 5 angles should produce at least one match
    assert "feature_vector" in angles_returned
    assert "workload_class" in angles_returned
    assert "predictor_state" in angles_returned
    assert "peak_signature" in angles_returned
    assert "recent_lineage" in angles_returned


def test_find_similar_excludes_self_by_ts(tmp_path):
    path = tmp_path / "incidents.jsonl"
    log_incident(_make(ts=200.0, emb=[1.0] + [0.0] * 25), path=path)
    new = _make(ts=200.0, emb=[1.0] + [0.0] * 25).to_dict()
    matches = find_similar(new, path=path)
    # The same ts must not appear as its own neighbour
    for m in matches:
        assert m["ts"] != 200.0
