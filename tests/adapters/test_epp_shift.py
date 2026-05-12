"""EppShift actuator tests — fakeroot tmp_path, no hardware writes."""

from __future__ import annotations

import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from coolstep.adapters.actuators.epp_shift import (
    DEFAULT_TARGET,
    EPP_FILE,
    EppShift,
    make,
)
from coolstep.core.schema import Action, ActionVerb

# ── helpers ───────────────────────────────────────────────────────────────

def _action(target: str | None = None, expires_in: float = 30.0) -> Action:
    params: dict[str, str | int | float] = {}
    if target is not None:
        params["target"] = target
    return Action(
        verb=ActionVerb.SHIFT_POWER_ENVELOPE,
        params=params,
        expires_at=time.time() + expires_in,
    )


def _make_cpu(cpu_root: Path, cpu_id: str, value: str = "balance_performance") -> Path:
    """Create fakeroot cpu dir with writable EPP file. Returns epp_path."""
    epp_dir = cpu_root / cpu_id / "cpufreq"
    epp_dir.mkdir(parents=True, exist_ok=True)
    epp_path = epp_dir / "energy_performance_preference"
    epp_path.write_text(value)
    return epp_path


def _read_epp(cpu_root: Path, cpu_id: str) -> str:
    return (cpu_root / cpu_id / EPP_FILE).read_text().strip()


# ── 1. supports ───────────────────────────────────────────────────────────

def test_supports_only_shift_power_envelope(tmp_path: Path):
    act = EppShift(cpu_root=tmp_path)
    assert act.supports(ActionVerb.SHIFT_POWER_ENVELOPE)
    assert not act.supports(ActionVerb.RAMP_COOLING)
    assert not act.supports(ActionVerb.CAP_BOOST)


# ── 2. make() returns None when no EPP file ───────────────────────────────

def test_make_returns_none_when_no_epp_file(tmp_path: Path):
    with patch("coolstep.adapters.actuators.epp_shift._host_supported", return_value=False):
        result = make()
    assert result is None


# ── 3. make() disabled via env ────────────────────────────────────────────

def test_make_returns_none_when_disabled_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "false")
    result = make()
    assert result is None


# ── 4. make() returns actuator when EPP writable ──────────────────────────

def test_make_returns_actuator_when_epp_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    _make_cpu(tmp_path, "cpu0")
    with patch("coolstep.adapters.actuators.epp_shift._host_supported", return_value=True):
        act = make()
    # make() uses default CPU_ROOT_DEFAULT, so just check it returned non-None
    assert act is not None
    # Direct ctor still works with custom cpu_root
    act2 = EppShift(cpu_root=tmp_path)
    assert act2 is not None


# ── 5. _ensure_baseline snapshots all writable cpus ──────────────────────

def test_ensure_baseline_snapshots_all_writable_cpus(tmp_path: Path):
    for cpu in ("cpu0", "cpu1", "cpu2"):
        _make_cpu(tmp_path, cpu, "balance_performance")
    act = EppShift(cpu_root=tmp_path)
    act._ensure_baseline()
    assert set(act._baseline.keys()) == {"cpu0", "cpu1", "cpu2"}
    assert all(v == "balance_performance" for v in act._baseline.values())


# ── 6. _ensure_baseline skips non-writable cpus ──────────────────────────

def test_ensure_baseline_skips_non_writable(tmp_path: Path):
    _make_cpu(tmp_path, "cpu0", "balance_performance")
    epp1 = _make_cpu(tmp_path, "cpu1", "balance_performance")
    _make_cpu(tmp_path, "cpu2", "balance_performance")
    # Make cpu1 EPP read-only
    epp1.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    act = EppShift(cpu_root=tmp_path)
    act._ensure_baseline()
    assert "cpu1" not in act._baseline
    assert "cpu0" in act._baseline
    assert "cpu2" in act._baseline
    # Restore so tmp_path cleanup works
    epp1.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


# ── 7. apply dry-run does not write ──────────────────────────────────────

def test_apply_dryrun_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    _make_cpu(tmp_path, "cpu0", "balance_performance")
    act = EppShift(cpu_root=tmp_path)
    act.apply(_action(target="balance_power"))
    assert _read_epp(tmp_path, "cpu0") == "balance_performance"


# ── 8. apply armed writes target ─────────────────────────────────────────

def test_apply_armed_writes_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    _make_cpu(tmp_path, "cpu0", "balance_performance")
    _make_cpu(tmp_path, "cpu1", "balance_performance")
    act = EppShift(cpu_root=tmp_path)
    result = act.apply(_action(target="balance_power"))
    assert result.error is None
    assert _read_epp(tmp_path, "cpu0") == "balance_power"
    assert _read_epp(tmp_path, "cpu1") == "balance_power"


# ── 9. apply clamps invalid target to default ────────────────────────────

def test_apply_clamps_invalid_target_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    _make_cpu(tmp_path, "cpu0", "balance_performance")
    act = EppShift(cpu_root=tmp_path)
    act.apply(_action(target="garbage"))
    assert _read_epp(tmp_path, "cpu0") == DEFAULT_TARGET


# ── 10. revert restores original ─────────────────────────────────────────

def test_revert_restores_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    _make_cpu(tmp_path, "cpu0", "balance_performance")
    act = EppShift(cpu_root=tmp_path)
    act.apply(_action(target="balance_power"))
    assert _read_epp(tmp_path, "cpu0") == "balance_power"
    act.revert()
    assert _read_epp(tmp_path, "cpu0") == "balance_performance"


# ── 11. revert dry-run does not write ────────────────────────────────────

def test_revert_dryrun_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    _make_cpu(tmp_path, "cpu0", "balance_performance")
    act = EppShift(cpu_root=tmp_path)
    # apply dry-run fills baseline but doesn't write hardware
    act.apply(_action(target="power"))
    # file still has original value
    assert _read_epp(tmp_path, "cpu0") == "balance_performance"
    # revert dry-run also must not write
    act.revert()
    assert _read_epp(tmp_path, "cpu0") == "balance_performance"


# ── 12. dry_run returns SimResult with expected_effect keys ──────────────

def test_dry_run_returns_simresult_with_target(tmp_path: Path):
    act = EppShift(cpu_root=tmp_path)
    action = _action(target="power")
    sim = act.dry_run(action)
    assert "cpu_power_delta_w" in sim.expected_effect
    assert sim.expected_effect["epp_target"] == "power"
    assert sim.confidence > 0.0
    assert sim.reverts_in >= 0.0
