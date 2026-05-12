"""ryzenadj CAP_BOOST actuator — AMD APU power-limit shaping.

Maps ActionVerb.CAP_BOOST → ryzenadj --fast-limit / --slow-limit / --stapm-limit
calls. Lowers the boost ceiling proportionally to action.params['intensity_pct']:
  intensity=5%   → -5W from baseline
  intensity=20%  → -20W from baseline

Baseline = current limits read via `ryzenadj -i` parse. Snapshot persists for
5 min (matches asusctl_fan_curve._baseline_ttl).

Discovery contract:
    1. `ryzenadj` binary on PATH
    2. `ryzenadj -i` succeeds (sudo not assumed — ryzenadj is suid in Arch's
       package). If returncode != 0 → make() returns None
    3. COOLSTEP_ACTUATOR_ENABLE ≠ "false" / "0" (shared kill switch)

apply() writes hardware only if COOLSTEP_ACTUATOR_ENABLE ∈ {"true","1"}.
Default = dry-run: logs cmd, never invokes ryzenadj write commands.

Revert: re-applies baseline limits via three --*-limit calls. Idempotent.

Sudoers entry
-------------
ryzenadj write commands need CAP_SYS_RAWIO. Arch's package no longer ships
SUID, so the actuator shells out through `sudo -n` and relies on a narrow
NOPASSWD rule. Install once per host:

    printf 'zzalli ALL=(root) NOPASSWD: /usr/bin/ryzenadj\n' \
      | sudo tee /etc/sudoers.d/coolstep-ryzenadj
    sudo chmod 0440 /etc/sudoers.d/coolstep-ryzenadj
    sudo visudo -c -f /etc/sudoers.d/coolstep-ryzenadj

The `-n` flag in apply()/revert() makes the call fail cleanly with a
non-zero returncode if the entry is missing, instead of hanging on a
password prompt. Reads (`ryzenadj -i`) stay direct — no root needed there.

Secure-boot blocker (TUF A15)
-----------------------------
On this user's ASUS TUF A15, `sudo -n ryzenadj -i` outputs
«PCI Bus is not writeable, check secure boot» and falls back to /dev/mem,
which the kernel blocks under lockdown when secure boot is on. Until one
of the following is true, the actuator stays effectively dry-run/no-op:

    (a) secure boot is disabled in BIOS, or
    (b) the `ryzen_smu` DKMS kernel module is built and loaded
        (preferred — keeps secure boot on).

Until either (a) or (b) is in place, CAP_BOOST actions log intent but
never modify hardware.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time

from coolstep.adapters.actuators._base import append_journal_event
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)

ENABLE_ENV = "COOLSTEP_ACTUATOR_ENABLE"
SUBPROCESS_TIMEOUT_S = 2.0
MIN_LIMIT_MW = 15_000  # 15W floor — below this the APU misbehaves
MAX_BIAS_W = 20  # never shave more than 20W


# ryzenadj -i output excerpt:
#   STAPM LIMIT          |    45.000 |    7.500 | slow-limit
#   PPT LIMIT FAST       |    65.000 |   12.345 | fast-limit
#   PPT LIMIT SLOW       |    45.000 |    9.876 | slow-limit
# We parse the leftmost number per known LIMIT prefix.
_RX_STAPM = re.compile(r"STAPM LIMIT\s*\|\s*([\d.]+)")
_RX_FAST = re.compile(r"PPT LIMIT FAST\s*\|\s*([\d.]+)")
_RX_SLOW = re.compile(r"PPT LIMIT SLOW\s*\|\s*([\d.]+)")


def _parse_limits(stdout: str) -> dict[str, int] | None:
    """Extract baseline limits in mW (W → mW × 1000). Returns None on failure."""
    m_stapm = _RX_STAPM.search(stdout)
    m_fast = _RX_FAST.search(stdout)
    m_slow = _RX_SLOW.search(stdout)
    if not (m_stapm and m_fast and m_slow):
        return None
    try:
        return {
            "stapm": int(float(m_stapm.group(1)) * 1000),
            "fast": int(float(m_fast.group(1)) * 1000),
            "slow": int(float(m_slow.group(1)) * 1000),
        }
    except ValueError:
        return None


class RyzenadjCap:
    name = "ryzenadj_cap_boost"

    def __init__(self, binary: str = "ryzenadj", baseline_ttl_s: float = 300.0) -> None:
        self._binary = binary
        self._baseline_ttl_s = baseline_ttl_s
        self._baseline: dict[str, int] | None = None
        self._baseline_at: float = 0.0

    def supports(self, verb: ActionVerb) -> bool:
        return verb == ActionVerb.CAP_BOOST

    def dry_run(self, action: Action) -> SimResult:
        intensity = self._intensity(action)
        return SimResult(
            expected_effect={
                "cpu_power_delta_w": -intensity,
                "fast_limit_delta_mw": -intensity * 1000,
            },
            confidence=0.65,
            reverts_in=max(0.0, action.expires_at - time.time()),
        )

    def apply(self, action: Action) -> ActionResult:
        intensity = self._intensity(action)
        baseline = self._ensure_baseline()
        if baseline is None:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=None,
                stdout_tail="", error="baseline snapshot failed",
            )
            self._journal(result, action)
            return result
        new_limits = self._biased_limits(baseline, intensity)
        # `ryzenadj --*-limit=...` needs CAP_SYS_RAWIO / root to write MSRs.
        # Arch's package no longer ships SUID, so we route writes through a
        # narrow NOPASSWD sudoers entry (see /etc/sudoers.d/coolstep-ryzenadj).
        # `-n` = non-interactive: if the entry is missing the call fails
        # cleanly with returncode != 0 instead of hanging on a password
        # prompt. Reads (`ryzenadj -i`) stay direct — no root needed there.
        cmd = [
            "sudo", "-n", self._binary,
            f"--stapm-limit={new_limits['stapm']}",
            f"--fast-limit={new_limits['fast']}",
            f"--slow-limit={new_limits['slow']}",
        ]
        cmd_str = " ".join(cmd)
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()

        if mode not in {"true", "1"}:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=f"DRY-RUN intensity={intensity}W "
                            f"fast: {baseline['fast'] / 1000:.1f}→{new_limits['fast'] / 1000:.1f}W",
            )
            log.info("ryzenadj_cap DRY-RUN: %s", cmd_str)
            self._journal(result, action)
            return result

        try:
            cp = subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            tail = cp.stdout[-200:].decode("utf-8", errors="replace") if cp.stdout else ""
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=tail,
            )
            log.info("ryzenadj_cap APPLIED: %s", cmd_str)
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or b"")[-200:].decode("utf-8", errors="replace")
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=tail, error=f"returncode={exc.returncode}",
            )
            log.warning("ryzenadj_cap FAILED (%d): %s", exc.returncode, tail)
        except (subprocess.TimeoutExpired, OSError) as exc:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail="", error=type(exc).__name__,
            )
            log.warning("ryzenadj_cap TIMEOUT/OSError: %r", exc)
        self._journal(result, action)
        return result

    def revert(self) -> None:
        if self._baseline is None:
            return
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()
        # Same sudo routing as apply() — see comment there. Read of baseline
        # was direct, but restoring baseline writes MSRs so it needs root.
        cmd = [
            "sudo", "-n", self._binary,
            f"--stapm-limit={self._baseline['stapm']}",
            f"--fast-limit={self._baseline['fast']}",
            f"--slow-limit={self._baseline['slow']}",
        ]
        cmd_str = " ".join(cmd)
        if mode not in {"true", "1"}:
            log.info("ryzenadj_cap DRY-RUN revert: %s", cmd_str)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": "DRY-RUN baseline restore", "cmd_executed": cmd_str,
            })
            return
        try:
            subprocess.run(
                cmd, capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=True,
            )
            log.info("ryzenadj_cap revert: %s", cmd_str)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": "baseline restore", "cmd_executed": cmd_str,
            })
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("ryzenadj_cap revert failed: %r", exc)
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": "baseline restore", "cmd_executed": cmd_str,
                "error": type(exc).__name__,
            })

    # ── internals ─────────────────────────────────────────────────────────

    def _intensity(self, action: Action) -> int:
        # intensity_pct comes in as percentage (5..20). Treat directly as «watts to shave».
        i = float(action.params.get("intensity_pct", 10.0))
        return max(5, min(MAX_BIAS_W, int(round(i))))

    def _biased_limits(self, baseline: dict[str, int], intensity_w: int) -> dict[str, int]:
        bias_mw = intensity_w * 1000
        return {
            "stapm": max(MIN_LIMIT_MW, baseline["stapm"] - bias_mw),
            "fast": max(MIN_LIMIT_MW, baseline["fast"] - bias_mw),
            "slow": max(MIN_LIMIT_MW, baseline["slow"] - bias_mw),
        }

    def _ensure_baseline(self) -> dict[str, int] | None:
        now = time.monotonic()
        if self._baseline is not None and (now - self._baseline_at) < self._baseline_ttl_s:
            return self._baseline
        parsed = self._read_current_limits()
        if parsed is not None:
            self._baseline = parsed
            self._baseline_at = now
        return self._baseline

    def _read_current_limits(self) -> dict[str, int] | None:
        try:
            cp = subprocess.run(
                [self._binary, "-i"], capture_output=True,
                timeout=SUBPROCESS_TIMEOUT_S, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("ryzenadj -i failed: %r", exc)
            return None
        if cp.returncode != 0:
            return None
        return _parse_limits(cp.stdout.decode("utf-8", errors="replace"))

    def _journal(self, result: ActionResult, action: Action) -> None:
        append_journal_event({
            "kind": "apply", "actuator": self.name,
            "applied_at": result.applied_at,
            "cmd_executed": result.cmd_executed,
            "stdout_tail": result.stdout_tail,
            "error": result.error,
            "verb": action.verb.value,
            "params": dict(action.params),
            "expires_at": action.expires_at,
        })


def _host_supported(binary: str = "ryzenadj") -> bool:
    if shutil.which(binary) is None:
        return False
    try:
        cp = subprocess.run(
            [binary, "-i"], capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return cp.returncode == 0


def make() -> RyzenadjCap | None:
    if os.environ.get(ENABLE_ENV, "dry-run").lower() in {"false", "0"}:
        return None
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not _c.ryzenadj_available:
        return None
    if not _host_supported():
        return None
    return RyzenadjCap()
