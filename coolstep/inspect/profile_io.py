"""Profile export/import — JSON blob capturing tuned-for-this-host state.

What's included:
- decision thresholds (notify_prob/soft_action_prob/hard_action_prob/min_confidence/
  min_arm_confidence/min_arm_labeled_count/rearm_gap_s/ramp-cooling slope guard/
  lookahead_sec)
- asusctl baseline curve snapshot from data/asusctl_fan_curve_baseline.json
- calibration targets (env-driven, env names + current values)
- core paths metadata (NOT actual chroma vectors — too heavy)

What's NOT included:
- chroma vectors (separate snapshot tool; portable across machines is iffy)
- runtime-state.json (host-specific armed actions, useless to import)
- actuator-journal.jsonl (host-specific history)

Schema v1:
{
  "version": 1,
  "exported_at": <unix ts>,
  "host_meta": {"profile": "Performance", "fan": "cpu"},
  "decision_thresholds": {
    "notify_prob": 0.4,
    "soft_action_prob": 0.7,
    "hard_action_prob": 0.9,
    "min_confidence": 0.6,
    "min_arm_confidence": 0.5,
    "min_arm_labeled_count": 5,
    "rearm_gap_s": 15.0,
    "ramp_cooling_temp_margin_c": 0.5,
    "ramp_cooling_cooling_slope_c_per_sec": -0.05,
    "lookahead_sec": 30.0
  },
  "asusctl_baseline": {
    "profile": "Performance",
    "fan": "cpu",
    "anchors": [[60.0, 0.0], [65.0, 10.0], ...],
    "saved_at": <ts>
  },
  "calibration_targets": {
    "COOLSTEP_COVERAGE_HOURS": "168",
    "COOLSTEP_THROTTLE_EVENTS": "10",
    ...
  }
}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _read_baseline(coolstep_home: Path) -> dict | None:
    bp = coolstep_home / "asusctl_fan_curve_baseline.json"
    if not bp.exists():
        return None
    try:
        return json.loads(bp.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _capture_calibration_env() -> dict[str, str]:
    """Snapshot all COOLSTEP_* env vars at export time."""
    return {k: v for k, v in os.environ.items() if k.startswith("COOLSTEP_")}


def export_profile(coolstep_home: Path) -> dict:
    """Build the profile dict. Pure — no I/O outside reading coolstep_home/."""
    from coolstep.core.decision import Thresholds

    t = Thresholds()
    baseline = _read_baseline(coolstep_home)

    return {
        "version": 1,
        "exported_at": time.time(),
        "host_meta": {
            "profile": baseline.get("profile") if baseline else "Performance",
            "fan": baseline.get("fan") if baseline else "cpu",
        },
        "decision_thresholds": {
            "notify_prob": t.notify_prob,
            "soft_action_prob": t.soft_action_prob,
            "hard_action_prob": t.hard_action_prob,
            "min_confidence": t.min_confidence,
            "min_arm_confidence": t.min_arm_confidence,
            "min_arm_labeled_count": t.min_arm_labeled_count,
            "rearm_gap_s": t.rearm_gap_s,
            "ramp_cooling_temp_margin_c": t.ramp_cooling_temp_margin_c,
            "ramp_cooling_cooling_slope_c_per_sec": (
                t.ramp_cooling_cooling_slope_c_per_sec
            ),
            "lookahead_sec": 30.0,  # daemon constant
        },
        "asusctl_baseline": baseline,
        "calibration_targets": _capture_calibration_env(),
    }


def import_profile(profile: dict, coolstep_home: Path, dry_run: bool = False) -> dict:
    """Apply profile to local install.

    Steps:
    1. Validate version == 1; else raise ValueError.
    2. Write asusctl_baseline (if present) to coolstep_home/asusctl_fan_curve_baseline.json.
    3. Suggest drop-in: return a systemd drop-in body the operator should paste into
       ~/.config/systemd/user/coolstep-collector.service.d/90-imported.conf
       containing the COOLSTEP_* env vars from calibration_targets.
    4. Decision thresholds are read at runtime from Thresholds() defaults; can't be
       persisted programmatically without code change. Return threshold_diff so the
       operator can decide whether to override via env.

    Returns:
        {
            "version": <profile["version"]>,
            "baseline_written": bool,
            "drop_in_suggested": <multi-line str>,
            "threshold_diff": dict of key -> (theirs, ours)  # diff vs local defaults
        }

    On dry_run=True: no files written, suggestions still populated, baseline_written=False.
    """
    if profile.get("version") != 1:
        raise ValueError(f"unsupported profile version: {profile.get('version')}")

    result: dict = {
        "version": 1,
        "baseline_written": False,
        "drop_in_suggested": "",
        "threshold_diff": {},
    }

    # 2. Write baseline
    baseline = profile.get("asusctl_baseline")
    if baseline and not dry_run:
        bp = coolstep_home / "asusctl_fan_curve_baseline.json"
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(baseline))
        result["baseline_written"] = True

    # 3. Generate drop-in body
    cal = profile.get("calibration_targets", {})
    if cal:
        lines = ["[Service]"]
        for k, v in sorted(cal.items()):
            lines.append(f"Environment={k}={v}")
        result["drop_in_suggested"] = "\n".join(lines)

    # 4. Compute threshold diff (imported value vs local default)
    from coolstep.core.decision import Thresholds

    local_t = Thresholds()
    their_t = profile.get("decision_thresholds", {})
    for k, their_v in their_t.items():
        local_v = getattr(local_t, k, None)
        if local_v is not None and local_v != their_v:
            result["threshold_diff"][k] = (their_v, local_v)

    return result
