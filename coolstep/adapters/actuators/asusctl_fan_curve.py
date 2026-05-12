"""asusctl fan-curve bias actuator — P2.0 dry-run.

Maps `ActionVerb.RAMP_COOLING` to an `asusctl fan-curve --mod-profile ... --fan cpu --data ...`
invocation, biasing the active fan curve upward in the 70-85°C knee band so
fans pre-spin **before** the reactive curve would naturally crest. Sandbox-
first: P2.0 always logs the constructed command and *never* runs subprocess
— P2.1 will gate hardware writes on `COOLSTEP_ACTUATOR_ENABLE`.

Bias formula (focused-on-knee):
    intensity_pct = clamp(action.params['intensity_pct'] * weight(anchor), 5, 20)
    weight(t) = trapezoid centred on 75-80°C — full inside, linear ramp 70 / 85,
                zero outside knee → preserves fan-stop band below 65°C and the
                reactive 90°C ceiling.
    final_pwm = clamp(baseline_pwm + applied_bias, baseline_pwm, 100)

Discovery contract:
    1. `asusctl` binary on PATH
    2. `asusctl info` reports v6+ (4.x/5.x CLI flag shapes differ)
    3. `COOLSTEP_ACTUATOR_ENABLE` ≠ "false" / "0"   (hard kill switch)

`apply()` writes to hardware only if `COOLSTEP_ACTUATOR_ENABLE` ∈ {"true","1"}.
Default is "dry-run" — logs the cmd, marks ActionResult with `stdout_tail="DRY-RUN"`.
P2.0 ships with the env unset → dry-run is the only path that's actually
reachable, the live `subprocess.run` branch is wired but unused.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from threading import RLock

from coolstep.adapters.actuators._base import append_journal_event
from coolstep.core.curve import (
    CurveContext,
    compose_curve,
    curve_signature,
    is_finite_curve,
)
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)

DEFAULT_PROFILE = os.environ.get("COOLSTEP_ASUSCTL_PROFILE", "Performance")
DEFAULT_FAN = os.environ.get("COOLSTEP_ASUSCTL_FAN", "cpu")
SUBPROCESS_TIMEOUT_S = 2.0
ENABLE_ENV = "COOLSTEP_ACTUATOR_ENABLE"
MIN_ASUSCTL_MAJOR = 6
# P2.4 — rate-limit re-applying the same curve. If `compose_curve` is
# byte-identical to the last issued curve, we skip the subprocess
# unless this many seconds have passed. Prevents asusctl spam when the
# context oscillates around a single curve shape.
MAX_REAPPLY_S = float(os.environ.get("COOLSTEP_ASUSCTL_MAX_REAPPLY_S", "60.0"))
KNEE_HOT = 80.0
KNEE_COOL = 75.0
KNEE_OUTER_HOT = 85.0
KNEE_OUTER_COOL = 70.0

# P2.4 — fallback CPU base curve used only when asusctl can't report any
# user curve. Anchored at 49°C as fan-stop (the kernel module's hw-floor
# on TUF A15 still gives ~2000 RPM there) and ramps gradually through the
# knee. The shape was tuned to keep noise low at office workloads
# (k10temp 50-60°C) while reserving aggressive PWM above 75°C.
DEFAULT_BASE_ANCHORS_CPU: tuple[tuple[float, float], ...] = (
    (49.0,   0.0),
    (56.0,   6.0),
    (64.0,  18.0),
    (70.0,  35.0),
    (75.0,  55.0),
    (80.0,  75.0),
    (85.0,  90.0),
    (90.0, 100.0),
)


def _knee_weight(temp_c: float) -> float:
    """Trapezoid weight: 1.0 inside 75-80°C; linear 0..1 outside up to 70/85; 0 beyond."""
    if KNEE_COOL <= temp_c <= KNEE_HOT:
        return 1.0
    if KNEE_OUTER_COOL <= temp_c < KNEE_COOL:
        return (temp_c - KNEE_OUTER_COOL) / (KNEE_COOL - KNEE_OUTER_COOL)
    if KNEE_HOT < temp_c <= KNEE_OUTER_HOT:
        return (KNEE_OUTER_HOT - temp_c) / (KNEE_OUTER_HOT - KNEE_HOT)
    return 0.0


def _bias_curve(
    anchors: list[tuple[float, float]],
    intensity_pct: float,
) -> list[tuple[float, float]]:
    """Return biased anchors. intensity_pct = top of the bias (e.g. 15 = +15% at knee)."""
    biased: list[tuple[float, float]] = []
    for temp_c, pwm_pct in anchors:
        bump = intensity_pct * _knee_weight(temp_c)
        final = max(pwm_pct, min(100.0, pwm_pct + bump))
        biased.append((temp_c, final))
    return biased


# Quiet-mode floor: never let an anchor drop below this PWM% in the knee
# band. Prevents the curve flat-lining and the chip silently soaking.
QUIET_MIN_PWM_PCT = 12.0
# Maximum subtraction at knee centre. Caps the quiet bias amplitude
# regardless of what the decision engine asks for — defensive bound.
QUIET_MAX_BIAS_PCT = 15.0


def _quiet_curve(
    anchors: list[tuple[float, float]],
    intensity_pct: float,
) -> list[tuple[float, float]]:
    """Return quiet-biased anchors — SUBTRACTS in the knee band.

    Same trapezoid weight as `_bias_curve` so the deepest cut sits at the
    centre of the 75-80°C knee and fades to zero outside 70/85°C. PWM is
    floored at `QUIET_MIN_PWM_PCT` so the chip is never left fanless.
    """
    intensity = max(0.0, min(QUIET_MAX_BIAS_PCT, intensity_pct))
    biased: list[tuple[float, float]] = []
    for temp_c, pwm_pct in anchors:
        cut = intensity * _knee_weight(temp_c)
        final = max(QUIET_MIN_PWM_PCT, pwm_pct - cut)
        # Never *raise* a curve point in quiet mode — only lower it.
        final = min(final, pwm_pct)
        biased.append((temp_c, final))
    return biased


def _format_data_arg(anchors: Iterable[tuple[float, float]]) -> str:
    """`asusctl --data` accepts `Xc:Y%`. Use integer °C + 1-dec PWM%."""
    return ",".join(f"{int(round(t))}c:{int(round(p))}%" for t, p in anchors)


# RON-ish blob parser. asusctl prints baseline curves as:
#     fan: CPU,
#     pwm: (20, 38, 71, 107, 140, 179, 217, 255),
#     temp: (55, 60, 65, 70, 75, 78, 80, 83),
_RX_FAN = re.compile(r"fan:\s*(\w+)", re.IGNORECASE)
_RX_PWM = re.compile(r"pwm:\s*\(([^)]*)\)")
_RX_TEMP = re.compile(r"temp:\s*\(([^)]*)\)")


def _parse_curve_block(block: str) -> tuple[str, list[tuple[float, float]]] | None:
    """Returns (fan_name, [(temp_c, pwm_pct), ...]). Returns None if malformed."""
    m_fan = _RX_FAN.search(block)
    m_pwm = _RX_PWM.search(block)
    m_temp = _RX_TEMP.search(block)
    if not (m_fan and m_pwm and m_temp):
        return None
    try:
        pwms = [float(x.strip()) for x in m_pwm.group(1).split(",") if x.strip()]
        temps = [float(x.strip()) for x in m_temp.group(1).split(",") if x.strip()]
    except ValueError:
        return None
    if len(pwms) != len(temps) or not pwms:
        return None
    # PWM is 0-255 in asusctl output; convert to percent.
    pwms_pct = [max(0.0, min(100.0, round(p / 255.0 * 100.0, 1))) for p in pwms]
    return m_fan.group(1).lower(), list(zip(temps, pwms_pct, strict=True))


def _parse_baseline(stdout: str, fan: str) -> list[tuple[float, float]] | None:
    """Pick the `fan` block out of `asusctl fan-curve --mod-profile X` output."""
    # asusctl renders each fan as its own `( ... )` block — split crude.
    # Block boundaries are `(` after blank line / start; conservative split by
    # «fan:» occurrences.
    if not stdout:
        return None
    pieces = re.split(r"(?=fan:\s*)", stdout, flags=re.IGNORECASE)
    for piece in pieces:
        parsed = _parse_curve_block(piece)
        if parsed is None:
            continue
        name, anchors = parsed
        if name == fan.lower():
            return anchors
    return None


def _parse_asusctl_major(info_stdout: str) -> int | None:
    # First line shape: `asusctl v6.3.6` (verified 2026-05-11).
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", info_stdout)
    return int(m.group(1)) if m else None


_GM_CACHE_TTL_S = 5.0
# Where baseline anchors get persisted so SIGKILL/OOM cleanup hook can restore
# them. Relative to COOLSTEP_HOME (= ~/coolstep/data by default).
BASELINE_FILE = "asusctl_fan_curve_baseline.json"


def _baseline_path() -> Path:
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / BASELINE_FILE


class AsusctlFanCurve:
    """Pre-emptive fan-curve bias driver.

    State that survives across `apply()` calls:
      _baseline_anchors — last-snapshotted curve (re-snapshotted if > 5min old)
      _baseline_at      — monotonic ts of snapshot
      _journal          — last N ActionResult for dashboard tile
      _gm_cache         — (is_active: bool, monotonic_ts: float) | None
    """

    name = "asusctl_fan_curve_bias"

    def __init__(
        self,
        profile: str = DEFAULT_PROFILE,
        fan: str = DEFAULT_FAN,
        binary: str = "asusctl",
        journal_capacity: int = 64,
        baseline_ttl_s: float = 300.0,
    ) -> None:
        self._profile = profile
        self._fan = fan
        self._binary = binary
        self._baseline_ttl = baseline_ttl_s
        self._baseline_anchors: list[tuple[float, float]] | None = None
        self._baseline_at: float = 0.0
        self._journal: deque[ActionResult] = deque(maxlen=journal_capacity)
        self._lock = RLock()
        self._gm_cache: tuple[bool, float] | None = None
        # P2.4 — last-issued curve signature + timestamp. Together they
        # implement the «skip subprocess if curve unchanged» rate-limit.
        self._last_curve_sig: str | None = None
        self._last_curve_at: float = 0.0

    # ── public ────────────────────────────────────────────────────────────

    def supports(self, verb: ActionVerb) -> bool:
        # Quiet-mode subtractive bias rides on the same `asusctl fan-curve`
        # surface as the cooling bias — one actuator, two directions.
        if verb not in (ActionVerb.RAMP_COOLING, ActionVerb.REDUCE_NOISE):
            return False
        return not (self._is_game_mode_active() and self._defer_to_game_mode())

    def dry_run(self, action: Action) -> SimResult:
        intensity = float(action.params.get("intensity_pct", 10.0))
        # Empirical estimate: each +10% bias in knee band ≈ −1.5°C peak Tctl
        # (from 2026-05-07 quietify-mid swap delta; revisit with stress-test data).
        expected_delta = -1.5 * (intensity / 10.0)
        ttl = max(0.0, action.expires_at - time.time())
        return SimResult(
            expected_effect={
                "cpu_temp_delta_c": expected_delta,
                "intensity_pct": intensity,
                "knee_band_c": [KNEE_COOL, KNEE_HOT],
            },
            confidence=0.55,  # empirical, not yet validated by stress-test
            reverts_in=ttl,
        )

    def apply(self, action: Action) -> ActionResult:
        intensity = float(action.params.get("intensity_pct", 10.0))
        is_quiet = action.verb == ActionVerb.REDUCE_NOISE
        baseline = self._ensure_baseline()
        if baseline is None:
            return self._record(ActionResult(
                applied_at=time.time(), cmd_executed=None,
                stdout_tail="", error="baseline snapshot failed",
            ), action)

        # P2.4 — adaptive path. If the action carries a `curve_context`
        # dict, run the policy pipeline. The legacy intensity_pct branch
        # is the fallback for callers (mostly tests) that don't supply
        # a context yet.
        ctx_raw = action.params.get("curve_context")
        adaptive = isinstance(ctx_raw, dict)

        if adaptive:
            ctx = CurveContext(
                cpu_temp_c=ctx_raw.get("cpu_temp_c"),
                throttle_prob=float(ctx_raw.get("throttle_prob", 0.0)),
                confidence=float(ctx_raw.get("confidence", 0.0)),
                expected_temp_c=ctx_raw.get("expected_temp_c"),
                horizon_sec=float(ctx_raw.get("horizon_sec", 30.0)),
                mode=str(ctx_raw.get("mode", "cool")),
                workload_class=ctx_raw.get("workload_class"),
                recent_throttle_count=int(ctx_raw.get("recent_throttle_count", 0)),
                armed_verbs=frozenset(ctx_raw.get("armed_verbs", ())),
                conservative=bool(ctx_raw.get("conservative", False)),
                # P2.5 — Heavy-1 fields. Profile defaults to "other" when
                # workload classifier hasn't fired yet; heat_soak_index
                # defaults to 0.0 so the bump policy is a no-op.
                workload_profile=str(ctx_raw.get("workload_profile", "other")),
                heat_soak_index=float(ctx_raw.get("heat_soak_index", 0.0)),
            )
            biased = compose_curve(baseline, ctx)
            sign = "~"
            if not is_finite_curve(biased):
                log.warning("%s: compose_curve produced non-finite anchors, falling back to baseline", self.name)
                biased = list(baseline)
            # Rate-limit: if the resulting curve matches the last issued
            # signature AND we re-applied less than MAX_REAPPLY_S ago,
            # skip the subprocess entirely — just journal the intent.
            sig = curve_signature(biased)
            now_mono = time.monotonic()
            if (
                self._last_curve_sig == sig
                and (now_mono - self._last_curve_at) < MAX_REAPPLY_S
            ):
                result = ActionResult(
                    applied_at=time.time(),
                    cmd_executed=None,
                    stdout_tail=f"SKIP same-curve sig={sig[:40]}…",
                )
                log.debug("%s: skip same-curve (sig=%s)", self.name, sig)
                return self._record(result, action)
        elif is_quiet:
            intensity = max(0.0, min(QUIET_MAX_BIAS_PCT, intensity))
            biased = _quiet_curve(baseline, intensity_pct=intensity)
            sign = "-"
        else:
            intensity = max(5.0, min(20.0, intensity))
            biased = _bias_curve(baseline, intensity_pct=intensity)
            sign = "+"
        # P2.4 — emit ONLY `--data` here. asusctl's CLI processes flags
        # left-to-right; if both `--data` and `--enable-fan-curves true`
        # are present on the same line, the enable flag is silently
        # ignored. The enable repair is handled by a SECOND subprocess
        # below (and by the daemon's startup `ensure_enabled` probe).
        # We observed the conflated form failing on 2026-05-12: CPU stuck
        # `enabled: false` even after our curve issued 0%/49°C.
        cmd = [
            self._binary, "fan-curve",
            "--mod-profile", self._profile,
            "--fan", self._fan,
            "--data", _format_data_arg(biased),
        ]
        cmd_str = " ".join(cmd)
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()

        if mode not in {"true", "1"}:
            # P2.0 default — DRY-RUN. Log cmd, never exec. Cache the
            # signature even in dry-run so the rate-limit fires
            # consistently between dry-run and armed modes (tests rely
            # on the same skip semantics).
            self._last_curve_sig = curve_signature(biased)
            self._last_curve_at = time.monotonic()
            result = ActionResult(
                applied_at=time.time(),
                cmd_executed=cmd_str,
                stdout_tail=(
                    f"DRY-RUN {action.verb.value} sign={sign}{intensity:.1f}% "
                    f"knee=[{KNEE_COOL}-{KNEE_HOT}]°C"
                ),
            )
            log.info(
                "%s DRY-RUN %s: %s (sign=%s, intensity=%.1f%%)",
                self.name, action.verb.value, cmd_str, sign, intensity,
            )
            return self._record(result, action)

        # P2.1+ live exec path — wired but only reachable when env is set.
        try:
            cp = subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            tail = cp.stdout[-200:].decode("utf-8", errors="replace") if cp.stdout else ""
            result = ActionResult(
                applied_at=time.time(),
                cmd_executed=cmd_str, stdout_tail=tail,
            )
            # P2.4 — remember what we just issued so the next tick can
            # rate-limit re-applying the same curve.
            self._last_curve_sig = curve_signature(biased)
            self._last_curve_at = time.monotonic()
            log.info("%s APPLIED: %s", self.name, cmd_str)
            # P2.4 — second subprocess: re-affirm `enabled: true`. Cheap
            # (asusctl no-ops if the flag is already true) and the only
            # reliable way to guarantee the kernel module honours the
            # curve we just wrote. See the apply-cmd comment above for
            # the rationale.
            try:
                subprocess.run(
                    [
                        self._binary, "fan-curve",
                        "--mod-profile", self._profile,
                        "--fan", self._fan,
                        "--enable-fan-curves", "true",
                    ],
                    capture_output=True,
                    timeout=SUBPROCESS_TIMEOUT_S, check=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                log.debug("post-apply enable-flag re-affirm failed: %r", exc)
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or b"")[-200:].decode("utf-8", errors="replace")
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=tail, error=f"returncode={exc.returncode}",
            )
            log.warning("%s FAILED (%d): %s", self.name, exc.returncode, tail)
        except (subprocess.TimeoutExpired, OSError) as exc:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail="", error=type(exc).__name__,
            )
            log.warning("%s TIMEOUT/OSError: %r", self.name, exc)
        return self._record(result, action)

    def revert(self) -> None:
        """Restore profile's fan-curve to the baseline snapshot taken at apply().

        Re-issues the **saved baseline** (the user's curve at the moment we
        first biased it) via `--data`, NOT factory `--default`. Factory reset
        wipes user-tuned curves like quietify-mid 2026-05-07 — verified live
        2026-05-12 incident, the reason this implementation prefers re-apply
        over `--default`.

        Fallback hierarchy:
          1. `_baseline_anchors` saved → re-apply via `--data` (preserves user curve)
          2. no baseline yet → `--default` (true factory reset, last resort)

        Idempotent — re-issuing the same `--data` twice is a no-op for asusctl.
        DRY-RUN mode just logs.
        """
        if self._baseline_anchors is not None:
            # P2.4 — like apply(), revert issues `--data` first, then
            # re-affirms the enable flag in a second subprocess below.
            cmd = [
                self._binary, "fan-curve",
                "--mod-profile", self._profile,
                "--fan", self._fan,
                "--data", _format_data_arg(self._baseline_anchors),
            ]
            reason = "restored baseline"
        else:
            cmd = [self._binary, "fan-curve",
                   "--mod-profile", self._profile, "--default"]
            reason = "factory --default (no baseline snapshot)"
        cmd_str = " ".join(cmd)
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()
        if mode not in {"true", "1"}:
            log.info("%s DRY-RUN revert: %s", self.name, cmd_str)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": f"DRY-RUN {reason}", "cmd_executed": cmd_str,
            })
            return
        try:
            subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            log.info("%s revert: %s (%s)", self.name, reason, cmd_str)
            # P2.4 — re-affirm enable flag (see apply() comment).
            if self._baseline_anchors is not None:
                try:
                    subprocess.run(
                        [
                            self._binary, "fan-curve",
                            "--mod-profile", self._profile,
                            "--fan", self._fan,
                            "--enable-fan-curves", "true",
                        ],
                        capture_output=True,
                        timeout=SUBPROCESS_TIMEOUT_S, check=False,
                    )
                except (subprocess.TimeoutExpired, OSError) as exc:
                    log.debug("revert enable-flag re-affirm failed: %r", exc)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": reason, "cmd_executed": cmd_str,
            })
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("%s revert failed: %r", self.name, exc)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": reason, "cmd_executed": cmd_str,
                "error": type(exc).__name__,
            })

    def journal(self) -> list[ActionResult]:
        with self._lock:
            return list(self._journal)

    def ensure_enabled(self) -> bool:
        """One-shot probe + repair: if the per-fan curve is currently
        ``enabled: false`` in asusctl, re-issue the baseline curve with
        ``--enable-fan-curves true`` to flip the flag without disturbing
        the anchors.

        Returns True if the flag is (or was made) ``enabled: true``,
        False on any failure. Called by the daemon at startup; the cost
        is one `asusctl fan-curve` read + at most one write.

        DRY-RUN compatible — if `COOLSTEP_ACTUATOR_ENABLE` is not set,
        the repair is logged but not executed.
        """
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()
        # Read current state regardless of dry-run — the probe itself is
        # safe (no write side-effects in asusctl).
        try:
            cp = subprocess.run(
                [self._binary, "fan-curve", "--mod-profile", self._profile],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT_S, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("ensure_enabled probe failed: %r", exc)
            return False
        if cp.returncode != 0:
            return False
        stdout = cp.stdout.decode("utf-8", errors="replace")
        # asusctl renders each fan as a RON-ish block — look for the
        # specific block matching `self._fan` and inspect its `enabled:` flag.
        block_marker = f"fan: {self._fan.upper()}"
        idx = stdout.find(block_marker)
        if idx == -1:
            log.debug("ensure_enabled: fan block %r not found in stdout", block_marker)
            return False
        # Walk forward line-by-line; the *first* `enabled:` we hit
        # belongs to this fan's block. (A 400-char chunk used to leak
        # into the *next* fan block, which on TUF A15 was GPU `enabled:
        # true` — making the probe think CPU was already enabled when
        # it wasn't.)
        block_enabled: bool | None = None
        for line in stdout[idx:].splitlines():
            stripped = line.strip()
            if stripped.startswith("enabled:"):
                block_enabled = "true" in stripped.lower()
                break
        if block_enabled is True:
            return True
        if block_enabled is None:
            log.debug("ensure_enabled: no `enabled:` line after %r", block_marker)
            return False
        # Need repair. Issue ONLY `--enable-fan-curves true` — see the
        # P2.4 comment in apply(): combining `--data` with the enable
        # flag in a single asusctl call silently ignores the flag.
        cmd = [
            self._binary, "fan-curve",
            "--mod-profile", self._profile,
            "--fan", self._fan,
            "--enable-fan-curves", "true",
        ]
        cmd_str = " ".join(cmd)
        if mode not in {"true", "1"}:
            log.info("%s DRY-RUN ensure_enabled: %s", self.name, cmd_str)
            return False
        try:
            subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            log.info("%s ensure_enabled: flipped %s to enabled (cmd=%s)",
                     self.name, self._fan, cmd_str)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("ensure_enabled apply failed: %r", exc)
            return False

    # ── internal ──────────────────────────────────────────────────────────

    def _is_game_mode_active(self) -> bool:
        """Return True if any game-mode-like service is active right now.

        Checks two units (S13 in interference-matrix.md):
          - `--user game-mode.service` — Hyprland-event-driven custom unit
          - system-level `gamemoded.service` — Feral gamemoded daemon

        Either positive triggers defer. Result is cached for `_GM_CACHE_TTL_S`
        seconds to avoid forking `systemctl` on every daemon tick. Any error
        or timeout is treated as «not active». Logs once per cache-window
        when the state flips to active.
        """
        now = time.monotonic()
        if self._gm_cache is not None:
            cached_active, cached_at = self._gm_cache
            if now - cached_at < _GM_CACHE_TTL_S:
                return cached_active

        prev_active = self._gm_cache[0] if self._gm_cache is not None else False

        def _probe(args: list[str]) -> bool:
            try:
                cp = subprocess.run(
                    args, capture_output=True, timeout=0.5, check=False,
                )
                return cp.returncode == 0 and cp.stdout.strip() == b"active"
            except Exception:  # noqa: BLE001
                return False

        is_active = (
            _probe(["systemctl", "--user", "is-active", "game-mode.service"])
            or _probe(["systemctl", "is-active", "gamemoded.service"])
        )

        if is_active and not prev_active:
            log.debug("game-mode active (game-mode.service or gamemoded.service), deferring")

        self._gm_cache = (is_active, now)
        return is_active

    def _defer_to_game_mode(self) -> bool:
        """Return True when coolstep should stay silent while game-mode is on.

        Reads env `COOLSTEP_GAME_MODE_DEFER`:
          - "0" / "false" -> False  (cooperative: apply bias on top of game-mode)
          - anything else including unset -> True  (default: defer, don't fire)
        """
        val = os.environ.get("COOLSTEP_GAME_MODE_DEFER", "1").lower()
        return val not in {"0", "false"}

    def _record(self, result: ActionResult, action: Action | None = None) -> ActionResult:
        with self._lock:
            self._journal.append(result)
        # Also persist to disk-jsonl so dashboard (separate process) sees it.
        record: dict = {
            "kind": "apply",
            "actuator": self.name,
            "applied_at": result.applied_at,
            "cmd_executed": result.cmd_executed,
            "stdout_tail": result.stdout_tail,
            "error": result.error,
        }
        if action is not None:
            record["verb"] = action.verb.value
            record["params"] = dict(action.params)
            record["expires_at"] = action.expires_at
        append_journal_event(record)
        return result

    def _ensure_baseline(self) -> list[tuple[float, float]] | None:
        now = time.monotonic()
        if (self._baseline_anchors is not None
                and (now - self._baseline_at) < self._baseline_ttl):
            return self._baseline_anchors
        anchors = self._read_current_curve()
        if anchors is None and self._fan.lower() == "cpu":
            # P2.4 — fallback to the conservative lower-floor base when
            # asusctl can't report any curve at all (e.g. the per-fan
            # curve was never seeded). Only applied for CPU; GPU keeps
            # the firmware default since the user's complaint is
            # CPU-fan-driven (see plan «Out of scope»).
            anchors = list(DEFAULT_BASE_ANCHORS_CPU)
            log.info("%s: using lower-floor default CPU base (asusctl read failed)",
                     self.name)
        if anchors is not None:
            self._baseline_anchors = anchors
            self._baseline_at = now
            self._persist_baseline(anchors)
        return self._baseline_anchors

    def _persist_baseline(self, anchors: list[tuple[float, float]]) -> None:
        """Write baseline snapshot to disk so `coolstep-cleanup.sh` can read
        it during SIGKILL/OOM recovery (Python `finally` doesn't run there).
        Best-effort — failures are logged at debug, never raised.
        """
        try:
            path = _baseline_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "profile": self._profile,
                "fan": self._fan,
                "anchors": [[t, p] for t, p in anchors],
                "saved_at": time.time(),
            }
            path.write_text(json.dumps(payload))
        except OSError as exc:
            log.debug("baseline persist failed: %r", exc)

    def _read_current_curve(self) -> list[tuple[float, float]] | None:
        try:
            cp = subprocess.run(
                [self._binary, "fan-curve", "--mod-profile", self._profile],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT_S, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("asusctl curve probe failed: %r", exc)
            return None
        if cp.returncode != 0:
            return None
        return _parse_baseline(cp.stdout.decode("utf-8", errors="replace"), self._fan)


def _host_supported(binary: str = "asusctl") -> bool:
    """Cheap discovery: binary on PATH + asusctl v6+."""
    if shutil.which(binary) is None:
        return False
    try:
        cp = subprocess.run(
            [binary, "info"], capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if cp.returncode != 0:
        return False
    major = _parse_asusctl_major(cp.stdout.decode("utf-8", errors="replace"))
    if major is None or major < MIN_ASUSCTL_MAJOR:
        log.info("asusctl: version too old (major=%s), need ≥ %d", major, MIN_ASUSCTL_MAJOR)
        return False
    return True


def make() -> AsusctlFanCurve | None:
    if os.environ.get(ENABLE_ENV, "dry-run").lower() in {"false", "0"}:
        return None
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not (_c.asusctl_available and _c.asusctl_version_ok):
        return None
    if not _host_supported():
        return None
    return AsusctlFanCurve()
