"""Tests for coolstep/inspect/profile_io.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coolstep.inspect.profile_io import export_profile, import_profile

_THRESHOLD_KEYS = {
    "notify_prob",
    "soft_action_prob",
    "hard_action_prob",
    "min_confidence",
    "min_arm_confidence",
    "min_arm_labeled_count",
    "rearm_gap_s",
    "ramp_cooling_temp_margin_c",
    "ramp_cooling_cooling_slope_c_per_sec",
    "lookahead_sec",
}

_FAKE_BASELINE = {
    "profile": "Performance",
    "fan": "cpu",
    "anchors": [[60.0, 0.0], [70.0, 50.0], [80.0, 100.0]],
    "saved_at": 1_700_000_000.0,
}


def test_export_includes_decision_thresholds(tmp_path: Path) -> None:
    result = export_profile(tmp_path)
    assert set(result["decision_thresholds"].keys()) == _THRESHOLD_KEYS


def test_export_includes_baseline_when_present(tmp_path: Path) -> None:
    (tmp_path / "asusctl_fan_curve_baseline.json").write_text(json.dumps(_FAKE_BASELINE))
    result = export_profile(tmp_path)
    assert result["asusctl_baseline"] == _FAKE_BASELINE


def test_export_baseline_none_when_missing(tmp_path: Path) -> None:
    result = export_profile(tmp_path)
    assert result["asusctl_baseline"] is None


def test_export_calibration_targets_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COOLSTEP_FOO", "bar")
    result = export_profile(tmp_path)
    assert result["calibration_targets"]["COOLSTEP_FOO"] == "bar"


def test_import_writes_baseline(tmp_path: Path) -> None:
    profile = {
        "version": 1,
        "asusctl_baseline": _FAKE_BASELINE,
        "calibration_targets": {},
        "decision_thresholds": {},
    }
    result = import_profile(profile, tmp_path, dry_run=False)
    assert result["baseline_written"] is True
    written = json.loads((tmp_path / "asusctl_fan_curve_baseline.json").read_text())
    assert written == _FAKE_BASELINE


def test_import_dry_run_does_not_write(tmp_path: Path) -> None:
    profile = {
        "version": 1,
        "asusctl_baseline": _FAKE_BASELINE,
        "calibration_targets": {"COOLSTEP_COVERAGE_HOURS": "168"},
        "decision_thresholds": {},
    }
    result = import_profile(profile, tmp_path, dry_run=True)
    assert result["baseline_written"] is False
    assert not (tmp_path / "asusctl_fan_curve_baseline.json").exists()
    assert "COOLSTEP_COVERAGE_HOURS=168" in result["drop_in_suggested"]
