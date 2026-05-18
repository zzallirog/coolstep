"""Summarise data/decisions.jsonl over a time range.

Fields per decision record (see coolstep/core/decision.py):
  ts, prediction:{...}, calibration_ready, labeled_count, actions[], thresholds{}

Summary:
  - count total decisions
  - count decisions that produced >=1 action ("fires")
  - count by verb (top 5)
  - distribution of throttle_prob (p10, p50, p90, max)
  - top 5 reasons (group-by reason string)
  - first/last ts in range
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any


def summarise(decisions_path: Path, since_seconds: float) -> dict[str, Any]:
    if not decisions_path.exists():
        return {"error": "decisions.jsonl missing"}
    cutoff = time.time() - since_seconds
    records: list[dict[str, Any]] = []
    try:
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if float(rec.get("ts", 0)) >= cutoff:
                records.append(rec)
    except OSError as exc:
        return {"error": str(exc)}

    if not records:
        return {
            "total": 0,
            "fires": 0,
            "verbs": {},
            "top_reasons": [],
            "prob_dist": {},
            "first_ts": None,
            "last_ts": None,
        }

    probs = [
        float(r["prediction"]["throttle_prob"])
        for r in records
        if r.get("prediction") and "throttle_prob" in r["prediction"]
    ]
    fires = sum(1 for r in records if r.get("actions"))
    verb_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    for r in records:
        for a in r.get("actions") or []:
            verb_counter[a.get("verb", "?")] += 1
        reason = (r.get("prediction") or {}).get("reason", "")
        if reason:
            reason_counter[reason] += 1

    return {
        "total": len(records),
        "fires": fires,
        "fire_rate": fires / max(1, len(records)),
        "verbs": dict(verb_counter.most_common(5)),
        "top_reasons": reason_counter.most_common(5),
        "prob_dist": {
            "p10": _quantile(probs, 0.10),
            "p50": _quantile(probs, 0.50),
            "p90": _quantile(probs, 0.90),
            "max": max(probs) if probs else 0.0,
        },
        "first_ts": min(r["ts"] for r in records if "ts" in r),
        "last_ts": max(r["ts"] for r in records if "ts" in r),
    }


def _quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    idx = max(0, min(len(xs) - 1, int(len(xs) * q)))
    return xs[idx]


def format_summary(s: dict[str, Any]) -> str:
    """Human-readable rendering."""
    if "error" in s:
        return f"history: {s['error']}"
    if s["total"] == 0:
        return "history: no decisions in range"

    lines: list[str] = []
    lines.append(f"decisions:    {s['total']:>6}")
    lines.append(f"fires:        {s['fires']:>6}  ({s['fire_rate']:.1%})")

    if s["verbs"]:
        lines.append("verbs:")
        for verb, count in s["verbs"].items():
            lines.append(f"  {verb:<24} {count}")

    p = s["prob_dist"]
    if p:
        lines.append(
            f"throttle_prob: p10={p['p10']:.2f} p50={p['p50']:.2f} "
            f"p90={p['p90']:.2f} max={p['max']:.2f}"
        )

    if s["top_reasons"]:
        lines.append("top reasons:")
        for reason, count in s["top_reasons"]:
            short = (reason[:70] + "...") if len(reason) > 70 else reason
            lines.append(f"  {count:>4}x {short}")
    return "\n".join(lines)
