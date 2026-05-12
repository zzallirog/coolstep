"""EPP shift actuator — soft power-envelope nudge via energy_performance_preference.

Maps ActionVerb.SHIFT_POWER_ENVELOPE to writes against
/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference.

Action params:
    target: "balance_power" | "power" (one of available values)

Defaults to "balance_power" if missing. Revert restores per-CPU original
(snapshot at first apply, persisted across applies within `_baseline_ttl`).

Discovery contract:
    1. cpu0's energy_performance_preference exists AND is writable
    2. cpu0's energy_performance_available_preferences contains expected values
    3. COOLSTEP_ACTUATOR_ENABLE ≠ "false" / "0" (shared kill switch with asusctl)

apply() writes to hardware only if COOLSTEP_ACTUATOR_ENABLE ∈ {"true","1"}.
Default = dry-run: logs cmd, never writes.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from coolstep.adapters.actuators._base import append_journal_event
from coolstep.core.schema import Action, ActionResult, ActionVerb, SimResult

log = logging.getLogger(__name__)

CPU_ROOT_DEFAULT = Path("/sys/devices/system/cpu")
ENABLE_ENV = "COOLSTEP_ACTUATOR_ENABLE"
EPP_FILE = "cpufreq/energy_performance_preference"
EPP_AVAILABLE_FILE = "cpufreq/energy_performance_available_preferences"
DEFAULT_TARGET = "balance_power"
VALID_TARGETS = ("performance", "balance_performance", "balance_power", "power")


class EppShift:
    name = "epp_shift"

    def __init__(self, cpu_root: Path = CPU_ROOT_DEFAULT) -> None:
        self._cpu_root = cpu_root
        self._baseline: dict[str, str] = {}  # cpu_id → original EPP value
        self._baseline_at: float = 0.0
        self._baseline_ttl_s: float = 300.0

    def supports(self, verb: ActionVerb) -> bool:
        return verb == ActionVerb.SHIFT_POWER_ENVELOPE

    def dry_run(self, action: Action) -> SimResult:
        target = str(action.params.get("target", DEFAULT_TARGET))
        return SimResult(
            expected_effect={"cpu_power_delta_w": -2.5, "epp_target": target},
            confidence=0.6,
            reverts_in=max(0.0, action.expires_at - time.time()),
        )

    def apply(self, action: Action) -> ActionResult:
        target = str(action.params.get("target", DEFAULT_TARGET))
        if target not in VALID_TARGETS:
            target = DEFAULT_TARGET

        # Snapshot baseline if stale
        self._ensure_baseline()
        if not self._baseline:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=None,
                stdout_tail="", error="no writable EPP cpus",
            )
            self._journal(result, action)
            return result

        cpus = sorted(self._baseline.keys())
        cmd_str = f"echo {target} | tee /sys/.../cpu*/{EPP_FILE} (n={len(cpus)})"
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()

        if mode not in {"true", "1"}:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=f"DRY-RUN target={target} cpus={len(cpus)}",
            )
            log.info("epp_shift DRY-RUN: target=%s cpus=%d", target, len(cpus))
            self._journal(result, action)
            return result

        # Live write
        errors: list[str] = []
        for cpu_id in cpus:
            epp_path = self._cpu_root / cpu_id / EPP_FILE
            try:
                epp_path.write_text(target)
            except OSError as exc:
                errors.append(f"{cpu_id}:{type(exc).__name__}")

        if errors:
            tail = ",".join(errors[:5])
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=tail, error=f"{len(errors)}/{len(cpus)} cpus failed",
            )
            log.warning("epp_shift partial fail: %s", tail)
        else:
            result = ActionResult(
                applied_at=time.time(), cmd_executed=cmd_str,
                stdout_tail=f"applied target={target} to {len(cpus)} cpus",
            )
            log.info("epp_shift APPLIED: target=%s cpus=%d", target, len(cpus))
        self._journal(result, action)
        return result

    def revert(self) -> None:
        if not self._baseline:
            return
        mode = os.environ.get(ENABLE_ENV, "dry-run").lower()
        if mode not in {"true", "1"}:
            log.info("epp_shift DRY-RUN revert: %d cpus", len(self._baseline))
            append_journal_event({
                "kind": "revert", "actuator": self.name,
                "reason": f"DRY-RUN restore {len(self._baseline)} cpus",
            })
            return
        restored = 0
        for cpu_id, original in self._baseline.items():
            try:
                (self._cpu_root / cpu_id / EPP_FILE).write_text(original)
                restored += 1
            except OSError as exc:
                log.debug("epp_shift revert cpu %s failed: %r", cpu_id, exc)
        log.info("epp_shift revert: restored %d/%d cpus", restored, len(self._baseline))
        append_journal_event({
            "kind": "revert", "actuator": self.name,
            "reason": f"restored {restored}/{len(self._baseline)} cpus",
        })

    # ── internal ──────────────────────────────────────────────────────────

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

    def _ensure_baseline(self) -> None:
        now = time.monotonic()
        if self._baseline and (now - self._baseline_at) < self._baseline_ttl_s:
            return
        snapshot: dict[str, str] = {}
        if not self._cpu_root.is_dir():
            return
        for child in sorted(self._cpu_root.glob("cpu[0-9]*")):
            epp_path = child / EPP_FILE
            if not epp_path.exists():
                continue
            try:
                value = epp_path.read_text().strip()
                # Verify writable
                if not os.access(epp_path, os.W_OK):
                    continue
                snapshot[child.name] = value
            except OSError:
                continue
        if snapshot:
            self._baseline = snapshot
            self._baseline_at = now


def _host_supported(cpu_root: Path = CPU_ROOT_DEFAULT) -> bool:
    """At minimum cpu0 has writable EPP file."""
    epp_p = cpu_root / "cpu0" / EPP_FILE
    if not epp_p.exists():
        return False
    return os.access(epp_p, os.W_OK)


def make() -> EppShift | None:
    if os.environ.get(ENABLE_ENV, "dry-run").lower() in {"false", "0"}:
        return None
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not _c.epp_available:
        return None
    if not _host_supported():
        return None
    return EppShift()
