"""Incident logger + multi-angle similarity — P2.4.

An *incident* is a moment worth remembering: a quiet-mode bias that had
to be ejected because the chip warmed up; a throttle FSM episode that
crossed the 90 °C trip; a RAMP_COOLING that had to push hard. Each one
is a candidate data point for "have we seen this pattern before, and
what happened then?".

Architecture:

* **Append-only journal** at ``data/incidents.jsonl`` (rotated at 5 MB).
  This is the source of truth. Every other view is derived.
* **Chroma collection `incidents`** sits alongside the existing `frames`
  collection. We reuse the same ``Embedder`` (26-dim) so feature-vector
  similarity is cheap and matches the predictor's universe.
* **Multi-angle similarity** (the user's «под несколькими углами»). The
  same new-incident is compared to prior incidents on five independent
  axes — feature embedding, workload class overlap, predictor-state
  proximity, peak signature, recent lineage. Each angle returns its own
  top-K with a short ``why`` explanation. Resists single-vector blind
  spots: two incidents can be cosine-close but workload-distant, which
  is exactly when you'd want the human to look.

Triggers (wired from `daemon.py`):

* ``Daemon._quiet_safety_eject``  → ``log_incident(kind="quiet_eject")``
* ``Daemon._throttle_fsm_tick`` close branch  → ``"throttle_close"``
* ``Daemon._do_apply`` on hard RAMP  → ``"ramp_fired_above_threshold"``

The daemon owns the trigger context. This module owns the persistence,
the similarity math, and the API shape.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INCIDENTS_FILE = "incidents.jsonl"
# 5 MB rotation — same generation count as actuator-journal so the
# audit-rotate semantics match across the codebase.
MAX_INCIDENTS_BYTES: int = int(os.environ.get("COOLSTEP_INCIDENTS_MAX_BYTES", "5000000"))
# Disable in tests to keep the user's real data dir clean. Mirrors the
# `COOLSTEP_DECISIONS_LOG_DISABLED` pattern already in decision.py.
DISABLED_ENV = "COOLSTEP_INCIDENTS_LOG_DISABLED"


def _incidents_path() -> Path:
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / INCIDENTS_FILE


def _rotate(path: Path) -> None:
    """3-generation rotation matching the actuator journal pattern.
    Best-effort: any OSError is logged + dropped, never raised."""
    gen2 = Path(str(path) + ".2")
    gen1 = Path(str(path) + ".1")
    try:
        if gen1.exists():
            os.rename(gen1, gen2)
        os.rename(path, gen1)
    except OSError as exc:
        log.debug("incidents log rotation failed: %r", exc)


@dataclass(slots=True)
class Incident:
    """One incident, ready for both jsonl + Chroma persistence."""
    ts: float
    kind: str                                   # "quiet_eject" | "throttle_close" | "ramp_fired_above_threshold"
    peak_temp_c: float
    duration_s: float
    workload_class: str | None
    top_processes: list[str]
    predictor_state: dict[str, Any]
    features: dict[str, float]
    mode_at_incident: str
    armed_verbs_at_incident: list[str]
    embedding: list[float] | None = None        # populated by daemon (uses Embedder)
    similar: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": float(self.ts),
            "kind": str(self.kind),
            "peak_temp_c": float(self.peak_temp_c),
            "duration_s": float(self.duration_s),
            "workload_class": self.workload_class,
            "top_processes": list(self.top_processes),
            "predictor_state": dict(self.predictor_state),
            "features": {k: float(v) for k, v in self.features.items()},
            "mode_at_incident": str(self.mode_at_incident),
            "armed_verbs_at_incident": list(self.armed_verbs_at_incident),
            "embedding": list(self.embedding) if self.embedding else None,
            "similar": list(self.similar),
        }


def log_incident(incident: Incident, *, path: Path | None = None) -> None:
    """Append one incident to the jsonl journal.

    Best-effort — any OSError is logged at debug level, never raised. The
    caller's trigger path (a hardware revert, a throttle FSM transition)
    must never fail because we couldn't write a log line. Tests disable
    writes entirely via ``COOLSTEP_INCIDENTS_LOG_DISABLED=1``."""
    if os.environ.get(DISABLED_ENV, "").lower() in {"1", "true", "yes"}:
        return
    target = path or _incidents_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size >= MAX_INCIDENTS_BYTES:
            _rotate(target)
        line = json.dumps(incident.to_dict(), separators=(",", ":"))
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        log.debug("incident write failed: %r", exc)


def read_incidents(*, path: Path | None = None, since_sec: float | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
    """Load the most recent ``limit`` incidents from jsonl, optionally
    filtered by `since_sec` (a Unix timestamp floor). Returns newest-first.

    Used by /api/incidents and by ``find_similar`` to walk prior records
    cheaply without round-tripping through Chroma for the angles that
    don't need embeddings."""
    target = path or _incidents_path()
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_sec is not None and float(row.get("ts", 0)) < since_sec:
                    continue
                rows.append(row)
    except OSError as exc:
        log.debug("incident read failed: %r", exc)
        return []
    rows.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
    return rows[:limit]


# ── Similarity — five independent angles ─────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity assuming both vectors are L2-normalised
    (which they are — Embedder enforces that). Returns 0 on any
    degenerate input so the caller can treat it as «no signal»."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))


def angle_feature_vector(
    current: dict[str, Any],
    prior: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Top-K by cosine distance over the 26-dim embedding.

    Reports each match with ``angle="feature_vector"``, distance =
    ``1 - cosine`` (so 0 = identical, 2 = opposite). ``why`` is a short
    plain-text explanation suitable for the dashboard.
    """
    emb = current.get("embedding")
    if not emb:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in prior:
        other = row.get("embedding")
        if not other:
            continue
        cos = _cosine(emb, other)
        dist = 1.0 - cos
        scored.append((dist, row))
    scored.sort(key=lambda x: x[0])
    return [
        {
            "ts": float(row["ts"]),
            "distance": float(dist),
            "angle": "feature_vector",
            "why": f"cosine={1.0 - dist:.3f} on 26-dim telemetry embedding",
        }
        for dist, row in scored[:top_k]
    ]


def angle_workload_class(
    current: dict[str, Any],
    prior: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Top-K by Jaccard over ``top_processes`` ∪ exact ``workload_class``
    match. Same workload class but disjoint top_processes still ranks
    above totally different processes — captures «similar app family
    but different exact binary»."""
    cur_class = (current.get("workload_class") or "").lower()
    cur_procs = set(p.lower() for p in current.get("top_processes", []) if p)
    scored: list[tuple[float, dict[str, Any], str]] = []
    for row in prior:
        oc = (row.get("workload_class") or "").lower()
        op = set(p.lower() for p in row.get("top_processes", []) if p)
        # Jaccard
        union = cur_procs | op
        inter = cur_procs & op
        jac = (len(inter) / len(union)) if union else 0.0
        # Exact class match adds a bonus that nudges past pure-Jaccard
        # ties for the same family without dominating cross-family hits.
        bonus = 0.3 if (cur_class and cur_class == oc) else 0.0
        score = jac + bonus
        if score == 0.0:
            continue
        why = (f"workload_class={oc} match · jaccard={jac:.2f}"
               if bonus else f"shared procs {sorted(inter)[:3]} · jaccard={jac:.2f}")
        scored.append((score, row, why))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "ts": float(row["ts"]),
            "distance": float(1.0 - score),
            "angle": "workload_class",
            "why": why,
        }
        for score, row, why in scored[:top_k]
    ]


def angle_predictor_state(
    current: dict[str, Any],
    prior: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Weighted L2 over the (throttle_prob, confidence, expected_temp_c)
    triple. Captures «predictor was equally wrong/right in this scene
    before» — useful for triaging «the model failed the same way
    again» vs «brand-new failure mode»."""
    cur = current.get("predictor_state") or {}
    if not cur:
        return []
    weights = {"throttle_prob": 1.0, "confidence": 0.5, "expected_temp_c": 0.02}
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in prior:
        other = row.get("predictor_state") or {}
        if not other:
            continue
        sq = 0.0
        for key, w in weights.items():
            a = cur.get(key)
            b = other.get(key)
            if a is None or b is None:
                continue
            sq += w * (float(a) - float(b)) ** 2
        scored.append((math.sqrt(sq), row))
    scored.sort(key=lambda x: x[0])
    return [
        {
            "ts": float(row["ts"]),
            "distance": float(dist),
            "angle": "predictor_state",
            "why": (
                f"Δprob={abs(float(cur.get('throttle_prob') or 0) - float(row['predictor_state'].get('throttle_prob') or 0)):.2f} "
                f"· Δconf={abs(float(cur.get('confidence') or 0) - float(row['predictor_state'].get('confidence') or 0)):.2f}"
            ),
        }
        for dist, row in scored[:top_k]
    ]


def angle_peak_signature(
    current: dict[str, Any],
    prior: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """``|Δpeak_temp| + |Δduration|`` — a coarse «physical signature»
    distance. Two incidents with similar peak + similar duration look
    physically alike regardless of how the predictor saw them."""
    cur_peak = float(current.get("peak_temp_c") or 0.0)
    cur_dur = float(current.get("duration_s") or 0.0)
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in prior:
        d = abs(float(row.get("peak_temp_c") or 0.0) - cur_peak) + abs(
            float(row.get("duration_s") or 0.0) - cur_dur
        )
        scored.append((d, row))
    scored.sort(key=lambda x: x[0])
    return [
        {
            "ts": float(row["ts"]),
            "distance": float(dist),
            "angle": "peak_signature",
            "why": (
                f"peak={float(row.get('peak_temp_c') or 0):.1f}°C "
                f"({float(row.get('peak_temp_c') or 0) - cur_peak:+.1f}) · "
                f"dur={float(row.get('duration_s') or 0):.1f}s"
            ),
        }
        for dist, row in scored[:top_k]
    ]


def angle_recent_lineage(
    current: dict[str, Any],
    prior: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Time-locality. Same 24 h slot or same weekday + hour-of-day are
    ranked above purely-random distance. Catches «every weekday at 14:00
    Steam syncs and the chip cooks» patterns that no feature/workload
    angle would surface."""
    cur_ts = float(current.get("ts") or time.time())
    scored: list[tuple[float, dict[str, Any], str]] = []
    cur_hour = int((cur_ts // 3600) % 24)
    cur_dow = int((cur_ts // 86400) % 7)
    for row in prior:
        other = float(row.get("ts") or 0.0)
        age = cur_ts - other
        if age <= 0:
            continue
        if age < 86400:
            scored.append((age / 86400.0, row, f"in the last 24 h ({age / 3600:.1f}h ago)"))
            continue
        other_hour = int((other // 3600) % 24)
        other_dow = int((other // 86400) % 7)
        if cur_hour == other_hour and cur_dow == other_dow:
            scored.append((1.5 + age / (7 * 86400.0), row, "same weekday + hour-of-day"))
        elif abs(cur_hour - other_hour) <= 1:
            scored.append((2.5 + age / (30 * 86400.0), row, f"close hour-of-day ({other_hour:02d}:00)"))
    scored.sort(key=lambda x: x[0])
    return [
        {
            "ts": float(row["ts"]),
            "distance": float(dist),
            "angle": "recent_lineage",
            "why": why,
        }
        for dist, row, why in scored[:top_k]
    ]


def find_similar(
    current: dict[str, Any],
    prior: list[dict[str, Any]] | None = None,
    *,
    top_k_per_angle: int = 3,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Run every angle and merge the results.

    Returns a flat list of ``{ts, distance, angle, why}`` entries, sorted
    by angle then by distance. The dashboard renders one row per (angle,
    rank) so the user sees, per angle, which prior incidents it picks.
    ``prior`` defaults to the last 500 incidents on disk.
    """
    if prior is None:
        prior = read_incidents(path=path, limit=500)
        # Exclude self if `current` is already on disk (i.e. caller wrote
        # before calling find_similar). Identity = same ts.
        cur_ts = float(current.get("ts") or 0.0)
        prior = [r for r in prior if float(r.get("ts") or 0.0) != cur_ts]
    out: list[dict[str, Any]] = []
    for angle_fn in (
        angle_feature_vector,
        angle_workload_class,
        angle_predictor_state,
        angle_peak_signature,
        angle_recent_lineage,
    ):
        out.extend(angle_fn(current, prior, top_k=top_k_per_angle))
    return out
