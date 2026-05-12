"""Per-actuator runtime detail — for `coolstep adapters --detailed`."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _coolstep_home() -> Path:
    return Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))


def _runtime_state() -> dict:
    p = _coolstep_home() / "runtime-state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _journal_count(actuator_name: str, limit: int = 1000) -> int:
    """Count journal entries matching actuator_name in last `limit` lines."""
    p = _coolstep_home() / "actuator-journal.jsonl"
    if not p.exists():
        return 0
    try:
        lines = p.read_text().splitlines()[-limit:]
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("actuator") == actuator_name:
            count += 1
    return count


def _baseline_info(actuator_name: str) -> dict:
    """For asusctl-style actuators, the baseline snapshot."""
    if actuator_name not in {"asusctl_fan_curve_bias", "game_mode_optimizer"}:
        return {}
    p = _coolstep_home() / "asusctl_fan_curve_baseline.json"
    if not p.exists():
        return {"baseline_present": False}
    try:
        b = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"baseline_present": False}
    age = time.time() - p.stat().st_mtime
    return {
        "baseline_present": True,
        "baseline_age_sec": age,
        "baseline_profile": b.get("profile"),
        "baseline_fan": b.get("fan"),
        "baseline_anchor_count": len(b.get("anchors") or []),
    }


def _armed_status(actuator_name: str, state: dict) -> dict:
    """Check runtime-state.json's armed_actions for this actuator."""
    armed_list = state.get("armed_actions") or []
    for entry in armed_list:
        if entry.get("actuator") == actuator_name:
            expires_at = float(entry.get("expires_at", 0))
            return {
                "armed": True,
                "verb": entry.get("verb"),
                "expires_at": expires_at,
                "ttl_remaining_sec": max(0.0, expires_at - time.time()),
            }
    return {"armed": False}


def detail_for(actuator) -> dict:
    """Compose detail dict for a single actuator instance."""
    from coolstep.core.schema import ActionVerb

    state = _runtime_state()
    verbs = [v.value for v in ActionVerb if actuator.supports(v)]

    return {
        "name": getattr(actuator, "name", "?"),
        "supports": verbs,
        "armed": _armed_status(actuator.name, state),
        "baseline": _baseline_info(actuator.name),
        "journal_count_last_1000": _journal_count(actuator.name),
    }


def detail_all(actuators) -> list[dict]:
    return [detail_for(a) for a in actuators]
