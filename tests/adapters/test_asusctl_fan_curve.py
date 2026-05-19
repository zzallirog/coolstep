"""asusctl_fan_curve actuator tests — P2.0 dry-run path."""

from __future__ import annotations

import json
import subprocess
import time
from unittest.mock import patch

from coolstep.adapters.actuators.asusctl_fan_curve import (
    KNEE_COOL,
    AsusctlFanCurve,
    _bias_curve,
    _format_data_arg,
    _knee_weight,
    _parse_asusctl_major,
    _parse_baseline,
    make,
)
from coolstep.core.schema import Action, ActionVerb

# Live snapshot of Performance curve on target (2026-05-11):
#   pwm:  (20, 38, 71, 107, 140, 179, 217, 255)
#   temp: (55, 60, 65, 70, 75, 78, 80, 83)
# After /255*100 → pwm_pct = (7.8, 14.9, 27.8, 42.0, 54.9, 70.2, 85.1, 100.0)
SAMPLE_BASELINE_OUTPUT = """
Fan curves for Performance

[
    (
        fan: CPU,
        pwm: (20, 38, 71, 107, 140, 179, 217, 255),
        temp: (55, 60, 65, 70, 75, 78, 80, 83),
        enabled: true,
    ),
    (
        fan: GPU,
        pwm: (10, 20, 40, 80, 120, 160, 200, 255),
        temp: (50, 58, 63, 68, 71, 74, 78, 83),
        enabled: true,
    ),
]
"""


def _completed(stdout: bytes = b"", returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["asusctl"], returncode=returncode, stdout=stdout, stderr=b"",
    )


# ── knee weight + bias formula ────────────────────────────────────────────

def test_knee_weight_full_inside_band():
    for t in (75.0, 77.5, 80.0):
        assert _knee_weight(t) == 1.0


def test_knee_weight_zero_outside_outer_band():
    assert _knee_weight(60.0) == 0.0
    assert _knee_weight(90.0) == 0.0
    assert _knee_weight(65.0) == 0.0


def test_knee_weight_linear_ramp_in_transition():
    # 70°C is the outer-cool boundary → 0; 72.5°C is midway → ~0.5
    assert _knee_weight(70.0) == 0.0
    assert abs(_knee_weight(72.5) - 0.5) < 1e-6
    # 85°C outer-hot → 0; 82.5°C midway → 0.5
    assert _knee_weight(85.0) == 0.0
    assert abs(_knee_weight(82.5) - 0.5) < 1e-6


def test_bias_curve_focused_on_knee():
    """Anchors at 60/65°C untouched; 75-80°C get full bump; 85°C zero."""
    baseline = [(60.0, 15.0), (75.0, 55.0), (80.0, 85.0), (85.0, 95.0)]
    biased = _bias_curve(baseline, intensity_pct=12.0)
    by_t = dict(biased)
    assert by_t[60.0] == 15.0       # outside band — untouched
    assert by_t[75.0] == 55.0 + 12.0
    assert by_t[80.0] == 85.0 + 12.0
    assert by_t[85.0] == 95.0       # outer edge — zero weight


def test_bias_curve_clamps_at_100():
    baseline = [(KNEE_COOL, 95.0)]
    biased = _bias_curve(baseline, intensity_pct=20.0)
    assert biased[0][1] == 100.0


def test_bias_curve_never_decreases_pwm():
    """Negative weights shouldn't ever happen — but verify clamp-at-baseline."""
    baseline = [(60.0, 50.0)]
    biased = _bias_curve(baseline, intensity_pct=10.0)
    assert biased[0][1] >= 50.0


# ── data arg formatting ──────────────────────────────────────────────────

def test_format_data_arg_shape():
    anchors = [(60.0, 0.0), (75.0, 55.0), (90.0, 100.0)]
    s = _format_data_arg(anchors)
    assert s == "60c:0%,75c:55%,90c:100%"


def test_format_data_arg_rounds():
    anchors = [(75.4, 55.6)]
    s = _format_data_arg(anchors)
    assert s == "75c:56%"


# ── baseline parser ───────────────────────────────────────────────────────

def test_parse_baseline_extracts_cpu_fan():
    anchors = _parse_baseline(SAMPLE_BASELINE_OUTPUT, "cpu")
    assert anchors is not None
    temps = [t for t, _ in anchors]
    assert temps == [55.0, 60.0, 65.0, 70.0, 75.0, 78.0, 80.0, 83.0]
    pwms = [p for _, p in anchors]
    # 255/255*100 = 100; 20/255*100 ≈ 7.8
    assert pwms[0] < 10.0
    assert pwms[-1] == 100.0


def test_parse_baseline_picks_gpu_when_asked():
    anchors = _parse_baseline(SAMPLE_BASELINE_OUTPUT, "gpu")
    assert anchors is not None
    temps = [t for t, _ in anchors]
    assert temps == [50.0, 58.0, 63.0, 68.0, 71.0, 74.0, 78.0, 83.0]


def test_parse_baseline_returns_none_on_garbage():
    assert _parse_baseline("not asusctl output", "cpu") is None
    assert _parse_baseline("", "cpu") is None


def test_parse_asusctl_major():
    assert _parse_asusctl_major("asusctl v6.3.6") == 6
    assert _parse_asusctl_major("asusctl v5.0.5") == 5
    assert _parse_asusctl_major("asusctl-bin 4.7.1") == 4
    assert _parse_asusctl_major("garbage") is None


# ── actuator behaviour: discovery + dry-run ───────────────────────────────

def test_make_returns_none_when_binary_missing(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    with patch("shutil.which", return_value=None):
        assert make() is None


def test_make_returns_none_when_version_too_old(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    with patch("shutil.which", return_value="/usr/bin/asusctl"), \
         patch("subprocess.run", return_value=_completed(stdout=b"asusctl v5.0.5\n")):
        assert make() is None


def test_make_returns_actuator_when_v6_present(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    with patch("shutil.which", return_value="/usr/bin/asusctl"), \
         patch("subprocess.run", return_value=_completed(stdout=b"asusctl v6.3.6\n")):
        a = make()
    assert a is not None
    assert a.name == "asusctl_fan_curve_bias"


def test_make_kill_switch_returns_none(monkeypatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "false")
    with patch("shutil.which", return_value="/usr/bin/asusctl"):
        assert make() is None


def test_supports_only_ramp_cooling():
    a = AsusctlFanCurve()
    assert a.supports(ActionVerb.RAMP_COOLING) is True
    assert a.supports(ActionVerb.CAP_BOOST) is False
    assert a.supports(ActionVerb.NOTIFY_USER) is False


def test_dry_run_returns_simresult():
    a = AsusctlFanCurve()
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 15.0},
        expires_at=time.time() + 30.0,
    )
    sim = a.dry_run(action)
    assert sim.expected_effect["cpu_temp_delta_c"] < 0
    assert sim.confidence > 0
    assert sim.reverts_in > 0


def test_apply_dryrun_never_subprocess_runs(monkeypatch):
    """P2.0 invariant: default mode = dry-run, subprocess.run for hardware
    write path is NEVER called (only for baseline read)."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve()
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 12.0},
        expires_at=time.time() + 30.0,
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        # Pretend it's the baseline-read call (the only legit one in dry-run):
        if "--mod-profile" in cmd and "--data" not in cmd:
            return _completed(stdout=SAMPLE_BASELINE_OUTPUT.encode())
        # The bias-write call should never reach here in dry-run; if it does,
        # we'll detect via the assertion below.
        raise AssertionError(f"unexpected subprocess.run in dry-run: {cmd}")

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)

    assert result.error is None
    assert result.cmd_executed is not None
    assert "--data" in result.cmd_executed
    assert result.stdout_tail.startswith("DRY-RUN")
    # Exactly one subprocess.run — the baseline read. The write was sandboxed.
    assert len(calls) == 1
    assert calls[0][1] == "fan-curve"
    assert "--data" not in calls[0]   # baseline read, not write


def test_apply_dryrun_records_to_journal(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve()
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 10.0},
        expires_at=time.time() + 30.0,
    )
    with patch("subprocess.run", return_value=_completed(stdout=SAMPLE_BASELINE_OUTPUT.encode())):
        a.apply(action)
        a.apply(action)
    journal = a.journal()
    assert len(journal) == 2
    assert all(r.stdout_tail.startswith("DRY-RUN") for r in journal)


def test_apply_dryrun_baseline_parse_fail_falls_back_to_default(monkeypatch):
    """P2.4 — when asusctl can't be parsed, CPU fan falls back to the
    lower-floor default curve (anchored at 49 °C / 0 %) so the dashboard
    still has something to apply instead of erroring out. The previous
    contract (error="baseline snapshot failed") is intentionally relaxed
    by Phase B."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve()  # default fan = "cpu"
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 10.0},
        expires_at=time.time() + 30.0,
    )
    with patch("subprocess.run", return_value=_completed(stdout=b"garbage")):
        result = a.apply(action)
    # Defaults landed → no error, DRY-RUN body issued, includes the new
    # 49°C floor anchor. The `--enable-fan-curves true` repair lives in a
    # separate subprocess (see apply() comment about asusctl ignoring
    # the flag when combined with --data), so we DON'T expect it here.
    assert result.error is None
    assert result.cmd_executed is not None
    assert "49c:0%" in result.cmd_executed


def test_apply_armed_invokes_subprocess(monkeypatch):
    """When COOLSTEP_ACTUATOR_ENABLE=true the bias cmd actually runs."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = AsusctlFanCurve()
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 12.0},
        expires_at=time.time() + 30.0,
    )

    seen_bias_call = []

    def fake_run(cmd, **_kw):
        if "--data" in cmd:
            seen_bias_call.append(list(cmd))
            return _completed(stdout=b"ok", returncode=0)
        return _completed(stdout=SAMPLE_BASELINE_OUTPUT.encode())

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)

    assert result.error is None
    assert len(seen_bias_call) == 1
    cmd = seen_bias_call[0]
    assert "fan-curve" in cmd
    assert "--mod-profile" in cmd
    assert "--data" in cmd
    # data arg contains biased 75-80°C anchors
    data_arg = cmd[cmd.index("--data") + 1]
    assert "75c:" in data_arg
    assert "80c:" in data_arg


def test_apply_armed_handles_subprocess_failure(monkeypatch):
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "1")
    a = AsusctlFanCurve()
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 10.0},
        expires_at=time.time() + 30.0,
    )

    def fake_run(cmd, **_kw):
        if "--data" in cmd:
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, stderr=b"asusctl: profile not active",
            )
        return _completed(stdout=SAMPLE_BASELINE_OUTPUT.encode())

    with patch("subprocess.run", side_effect=fake_run):
        result = a.apply(action)

    assert result.error is not None
    assert "returncode=1" in result.error


def test_revert_dryrun_logs_only(monkeypatch):
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve()
    with patch("subprocess.run") as mock_run:
        a.revert()
    assert mock_run.call_count == 0


def test_revert_armed_falls_back_to_default_without_baseline(monkeypatch):
    """Без baseline snapshot revert делает factory --default (last resort)."""
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = AsusctlFanCurve()
    # no apply() called → _baseline_anchors stays None
    with patch("subprocess.run", return_value=_completed(stdout=b"ok")) as mock_run:
        a.revert()
    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert "--default" in cmd


def test_revert_armed_restores_baseline_when_snapshot_present(monkeypatch):
    """С baseline snapshot revert re-applies сохранённую curve через --data
    (не factory --default — иначе стираем user-tuned quietify-mid curve).

    Регрессия 2026-05-12: prod-armed restart фактически дважды стёр
    юзеровский 2026-05-07 quietify-mid curve через --default revert path.
    """
    monkeypatch.setenv("COOLSTEP_ACTUATOR_ENABLE", "true")
    a = AsusctlFanCurve()
    # Simulate apply() having captured baseline
    a._baseline_anchors = [(60.0, 0.0), (75.0, 55.0), (80.0, 75.0), (90.0, 100.0)]
    a._baseline_at = time.monotonic()
    with patch("subprocess.run", return_value=_completed(stdout=b"ok")) as mock_run:
        a.revert()
    # P2.4 — revert now fires TWO subprocesses: first writes `--data`
    # with the saved baseline, then re-affirms `--enable-fan-curves
    # true` so the kernel module honours it. Combining the two on one
    # asusctl command silently drops the enable flag.
    assert mock_run.call_count == 2
    data_cmd = mock_run.call_args_list[0][0][0]
    assert "--default" not in data_cmd
    assert "--data" in data_cmd
    data_arg = data_cmd[data_cmd.index("--data") + 1]
    assert "60c:0%" in data_arg
    assert "75c:55%" in data_arg
    assert "90c:100%" in data_arg
    # The follow-up call must carry the enable-flag.
    enable_cmd = mock_run.call_args_list[1][0][0]
    assert "--enable-fan-curves" in enable_cmd
    assert "true" in enable_cmd


def test_baseline_persisted_to_disk_on_snapshot(monkeypatch, tmp_path):
    """`_ensure_baseline` writes `asusctl_fan_curve_baseline.json` so the
    SIGKILL cleanup hook (coolstep-cleanup.sh) can restore the user curve.

    Регрессия 2026-05-12: cleanup.sh без baseline file делал factory
    `--default` → стирал user-tuned quietify-mid. Persist даёт ему
    точку восстановления.
    """
    import json as _json
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve()
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 10.0},
        expires_at=time.time() + 30.0,
    )
    with patch("subprocess.run",
               return_value=_completed(stdout=SAMPLE_BASELINE_OUTPUT.encode())):
        a.apply(action)
    baseline_file = tmp_path / "asusctl_fan_curve_baseline.json"
    assert baseline_file.exists(), "baseline JSON must be written after first apply"
    payload = _json.loads(baseline_file.read_text())
    assert payload["profile"] == "Performance"
    assert payload["fan"] == "cpu"
    assert len(payload["anchors"]) == 8  # 8 anchor pairs in SAMPLE
    temps = [a[0] for a in payload["anchors"]]
    assert temps == [55.0, 60.0, 65.0, 70.0, 75.0, 78.0, 80.0, 83.0]


def test_baseline_persist_falls_back_to_default_for_cpu(monkeypatch, tmp_path):
    """P2.4 — when asusctl returns unparsable output, CPU fan falls back
    to `DEFAULT_BASE_ANCHORS_CPU` and the baseline is persisted from
    there. (Previous test asserted no file written; the new fallback
    semantics intentionally write the default so subsequent restarts
    can still recover via the persisted baseline.)"""
    monkeypatch.setenv("COOLSTEP_HOME", str(tmp_path))
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve()  # fan="cpu"
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 10.0},
        expires_at=time.time() + 30.0,
    )
    with patch("subprocess.run", return_value=_completed(stdout=b"garbage")):
        a.apply(action)
    baseline_file = tmp_path / "asusctl_fan_curve_baseline.json"
    assert baseline_file.exists()
    payload = json.loads(baseline_file.read_text())
    temps = [t for t, _ in payload["anchors"]]
    # The new default starts at 49 °C, not 60 °C.
    assert temps[0] == 49.0


def test_baseline_cache_reused_within_ttl(monkeypatch):
    """Two apply() calls within baseline_ttl share one curve-read subprocess."""
    monkeypatch.delenv("COOLSTEP_ACTUATOR_ENABLE", raising=False)
    a = AsusctlFanCurve(baseline_ttl_s=60.0)
    action = Action(
        verb=ActionVerb.RAMP_COOLING,
        params={"intensity_pct": 10.0},
        expires_at=time.time() + 30.0,
    )
    with patch("subprocess.run",
               return_value=_completed(stdout=SAMPLE_BASELINE_OUTPUT.encode())) as mock_run:
        a.apply(action)
        a.apply(action)
    # Only ONE baseline-read invocation
    assert mock_run.call_count == 1


# ── game-mode probe ───────────────────────────────────────────────────────

def test_supports_returns_true_when_game_mode_inactive(monkeypatch):
    """game-mode.service inactive -> supports(RAMP_COOLING) is True."""
    monkeypatch.delenv("COOLSTEP_GAME_MODE_DEFER", raising=False)
    a = AsusctlFanCurve()
    with patch("subprocess.run", return_value=_completed(stdout=b"inactive", returncode=3)):
        assert a.supports(ActionVerb.RAMP_COOLING) is True


def test_supports_returns_false_when_game_mode_active_with_defer(monkeypatch):
    """game-mode active + DEFER=1 (default) -> supports returns False."""
    monkeypatch.setenv("COOLSTEP_GAME_MODE_DEFER", "1")
    a = AsusctlFanCurve()
    with patch("subprocess.run", return_value=_completed(stdout=b"active", returncode=0)):
        assert a.supports(ActionVerb.RAMP_COOLING) is False


def test_supports_returns_true_when_game_mode_active_with_cooperate(monkeypatch):
    """game-mode active + DEFER=0 -> cooperative mode, supports returns True."""
    monkeypatch.setenv("COOLSTEP_GAME_MODE_DEFER", "0")
    a = AsusctlFanCurve()
    with patch("subprocess.run", return_value=_completed(stdout=b"active", returncode=0)):
        assert a.supports(ActionVerb.RAMP_COOLING) is True


def test_supports_cache_reuses_within_5s(monkeypatch):
    """Two supports() calls within 5s share one cached game-mode probe result.

    After the 2026-05-12 Feral gamemoded fix, one game-mode probe = up to two
    subprocess.run calls (game-mode.service then gamemoded.service fallback),
    but both probes happen inside _is_game_mode_active so the cache covers
    them together. We verify cache reuse via the *second* supports() call
    issuing zero additional subprocess calls.
    """
    monkeypatch.delenv("COOLSTEP_GAME_MODE_DEFER", raising=False)
    a = AsusctlFanCurve()
    with patch("subprocess.run",
               return_value=_completed(stdout=b"inactive", returncode=3)) as mock_run:
        a.supports(ActionVerb.RAMP_COOLING)
        calls_after_first = mock_run.call_count
        a.supports(ActionVerb.RAMP_COOLING)
        calls_after_second = mock_run.call_count
    # Second call must add zero probes — cache is the contract here.
    assert calls_after_second == calls_after_first
    # And the first probe ran (1 or 2 calls depending on fallback path)
    assert 1 <= calls_after_first <= 2


def test_supports_cache_expires_after_5s(monkeypatch):
    """Cache TTL expiry causes a fresh probe pair on the second call."""
    monkeypatch.delenv("COOLSTEP_GAME_MODE_DEFER", raising=False)
    a = AsusctlFanCurve()
    t0 = 1000.0
    _module = "coolstep.adapters.actuators.asusctl_fan_curve"
    with patch(f"{_module}.subprocess.run",
               return_value=_completed(stdout=b"inactive", returncode=3)) as mock_run, \
         patch(f"{_module}.time.monotonic", side_effect=[t0, t0 + 6.0]):
        a.supports(ActionVerb.RAMP_COOLING)
        first = mock_run.call_count
        a.supports(ActionVerb.RAMP_COOLING)
        second = mock_run.call_count
    # Re-probe happened: second pass added at least one call.
    assert second > first


def test_supports_other_verbs_always_false(monkeypatch):
    """Non-RAMP_COOLING verbs return False regardless of game-mode state."""
    monkeypatch.delenv("COOLSTEP_GAME_MODE_DEFER", raising=False)
    a = AsusctlFanCurve()
    # No subprocess.run call should be made for non-RAMP_COOLING verbs
    with patch("subprocess.run") as mock_run:
        assert a.supports(ActionVerb.CAP_BOOST) is False
        assert a.supports(ActionVerb.NOTIFY_USER) is False
    assert mock_run.call_count == 0


def test_supports_subprocess_failure_treated_as_inactive(monkeypatch):
    """TimeoutExpired during game-mode probe -> treated as not-active -> True."""
    monkeypatch.delenv("COOLSTEP_GAME_MODE_DEFER", raising=False)
    a = AsusctlFanCurve()
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=0.5)):
        assert a.supports(ActionVerb.RAMP_COOLING) is True
