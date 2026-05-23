"""coolstep doctor — structured diagnostic checks.

Each check returns a CheckResult with:
  status: "ok" | "warn" | "fail"
  message: human-readable detail

Aggregate verdict:
  any fail → exit 2
  any warn → exit 1
  else      → exit 0
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


@dataclass
class CheckResult:
    name: str
    status: str       # "ok" | "warn" | "fail"
    message: str
    detail: dict | None = field(default=None)


def _coolstep_home() -> Path:
    return Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))


def _linger_enabled() -> bool | None:
    """Return True/False/None — None means we couldn't query (no loginctl)."""
    try:
        cp = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger"],
            capture_output=True, timeout=2, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = cp.stdout.decode(errors="replace").strip()
    if "Linger=" not in out:
        return None
    return out.split("=", 1)[1].strip().lower() == "yes"


def check_systemd_session() -> CheckResult:
    """Gate that the headless prerequisites for `systemctl --user` are met.

    Runs before unit-state / dashboard-api checks because both depend on
    a live user manager. Misses surface the actual one-liner fix
    (loginctl enable-linger + XDG_RUNTIME_DIR export) instead of the
    cascade of unrelated 'inactive' / 'connection refused' downstream.

    Probes:
      * XDG_RUNTIME_DIR set + points at an existing directory
      * Linger flag for the current uid (so the user manager survives logout)
      * /run/user/$UID/coolstep/ creatable+writeable (hot-state mmap path)
    """
    fix = (
        "  sudo loginctl enable-linger $USER\n"
        f"  export XDG_RUNTIME_DIR=/run/user/{os.getuid()}"
    )
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not xdg:
        return CheckResult(
            "systemd_session", "warn",
            f"XDG_RUNTIME_DIR unset — no user manager on this shell; fix:\n{fix}",
        )
    runtime_dir = Path(xdg)
    if not runtime_dir.is_dir():
        return CheckResult(
            "systemd_session", "warn",
            f"XDG_RUNTIME_DIR={xdg} but directory missing; fix:\n{fix}",
        )
    linger = _linger_enabled()
    if linger is False:
        return CheckResult(
            "systemd_session", "warn",
            f"loginctl Linger=no — user manager dies on logout; fix:\n{fix}",
        )
    # Hot-state mmap path. Daemon creates this lazily; we attempt to do
    # the same so an unwriteable runtime_dir (rare but seen on weird
    # PAM setups) surfaces here instead of via a daemon crash later.
    hot_dir = runtime_dir / "coolstep"
    try:
        hot_dir.mkdir(exist_ok=True)
        probe = hot_dir / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            "systemd_session", "warn",
            f"{hot_dir} not writeable ({exc.__class__.__name__}: {exc}); "
            "mmap hot-state will fall back to $TMPDIR",
        )
    linger_msg = "linger=on" if linger else "linger=?"
    return CheckResult(
        "systemd_session", "ok",
        f"XDG_RUNTIME_DIR={xdg} · {linger_msg} · {hot_dir} writeable",
    )


def check_store_exists() -> CheckResult:
    p = _coolstep_home() / "store.db"
    if not p.exists():
        return CheckResult("store_exists", "fail", f"{p} not found — daemon never ran?")
    size_mb = p.stat().st_size / (1024 * 1024)
    return CheckResult("store_exists", "ok", f"{p} present ({size_mb:.1f} MB)")


def check_ml_state_fresh() -> CheckResult:
    p = _coolstep_home() / "ml-state.json"
    if not p.exists():
        return CheckResult("ml_state_fresh", "fail", "ml-state.json missing")
    age = time.time() - p.stat().st_mtime
    if age > 60:
        return CheckResult("ml_state_fresh", "warn",
                           f"ml-state stale ({age:.0f}s) — daemon paused/dead?")
    return CheckResult("ml_state_fresh", "ok", f"{age:.0f}s old")


def check_collector_unit() -> CheckResult:
    try:
        cp = subprocess.run(
            ["systemctl", "--user", "is-active", "coolstep-collector.service"],
            capture_output=True, timeout=2, check=False,
        )
        state = cp.stdout.decode().strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult("collector_unit", "fail", f"systemctl query failed: {exc!r}")
    if state == "active":
        return CheckResult("collector_unit", "ok", "active")
    return CheckResult("collector_unit", "fail", f"state={state}")


def check_dashboard_unit() -> CheckResult:
    try:
        cp = subprocess.run(
            ["systemctl", "--user", "is-active", "coolstep-dashboard.service"],
            capture_output=True, timeout=2, check=False,
        )
        state = cp.stdout.decode().strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult("dashboard_unit", "warn", f"query failed: {exc!r}")
    if state == "active":
        return CheckResult("dashboard_unit", "ok", "active")
    return CheckResult("dashboard_unit", "warn", f"state={state} (degraded UX)")


def check_dashboard_api() -> CheckResult:
    """Hit /api/health via stdlib urllib (no extra deps)."""
    try:
        # 15 s timeout: dashboard runs under CPUQuota and serves SSE in parallel;
        # /api/health can queue behind in-flight requests on busy hosts.
        # Higher than this and the doctor check itself feels broken.
        with urlopen("http://127.0.0.1:18889/api/health", timeout=15) as r:
            data = json.loads(r.read().decode())
    except (URLError, json.JSONDecodeError, TimeoutError) as exc:
        return CheckResult("dashboard_api", "warn", f"unreachable: {exc!r}")
    if not data.get("daemon_seen"):
        return CheckResult("dashboard_api", "fail", "daemon_seen=false")
    age = data.get("ml_state_age_sec")
    if age and age > 60:
        return CheckResult("dashboard_api", "warn", f"ml_state {age:.0f}s old")
    return CheckResult(
        "dashboard_api",
        "ok",
        f"daemon_seen ✓ ml_age={age:.1f}s" if age else "daemon_seen ✓",
    )


def check_baseline_present_if_armed() -> CheckResult:
    """If actuator armed, baseline file must exist (else hard-crash recovery wipes user curve)."""
    if os.environ.get("COOLSTEP_ACTUATOR_ENABLE", "dry-run").lower() not in {"true", "1"}:
        return CheckResult("baseline_present", "ok", "(dry-run mode — baseline check skipped)")
    p = _coolstep_home() / "asusctl_fan_curve_baseline.json"
    if not p.exists():
        return CheckResult("baseline_present", "warn",
                           "armed mode but no baseline snapshot yet — first apply will create it")
    return CheckResult("baseline_present", "ok", f"{p} present")


def check_runtime_state_no_stuck_armed() -> CheckResult:
    """runtime-state.json shouldn't have entries from a previous crash that didn't clear."""
    p = _coolstep_home() / "runtime-state.json"
    if not p.exists():
        return CheckResult("runtime_state", "ok", "no runtime-state yet (fresh)")
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("runtime_state", "warn", f"parse: {exc!r}")
    armed = data.get("armed_actions") or []
    if not armed:
        return CheckResult("runtime_state", "ok", "no stuck armed_actions")
    now = time.time()
    stale = [e for e in armed if float(e.get("expires_at", 0)) + 60 < now]
    if stale:
        return CheckResult("runtime_state", "fail",
                           f"{len(stale)} stale armed_actions — daemon may have crashed mid-bias")
    return CheckResult("runtime_state", "ok", f"{len(armed)} live armed actions")


def check_journal_rotation_healthy() -> CheckResult:
    """Journal jsonl shouldn't be > 5MB (rotation should keep it <= 1MB threshold)."""
    p = _coolstep_home() / "actuator-journal.jsonl"
    if not p.exists():
        return CheckResult("journal_rotation", "ok", "no journal yet")
    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > 5.0:
        return CheckResult("journal_rotation", "fail",
                           f"active journal {size_mb:.1f} MB > 5MB — rotation broken?")
    if size_mb > 1.5:
        return CheckResult("journal_rotation", "warn",
                           f"active journal {size_mb:.1f} MB > 1.5MB — rotation overdue")
    return CheckResult("journal_rotation", "ok", f"{size_mb:.2f} MB")


CHECKS = [
    # systemd_session runs first: if the user manager isn't reachable,
    # collector_unit/dashboard_unit/dashboard_api will all fail in ways
    # that hide the real fix (loginctl enable-linger).
    check_systemd_session,
    check_store_exists,
    check_ml_state_fresh,
    check_collector_unit,
    check_dashboard_unit,
    check_dashboard_api,
    check_baseline_present_if_armed,
    check_runtime_state_no_stuck_armed,
    check_journal_rotation_healthy,
]


def run_all() -> tuple[list[CheckResult], int]:
    """Run every check. Return (results, exit_code: 0=ok, 1=warn, 2=fail)."""
    results: list[CheckResult] = []
    worst = "ok"
    for fn in CHECKS:
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001
            r = CheckResult(fn.__name__, "fail", f"check raised: {type(exc).__name__}: {exc!r}")
        results.append(r)
        if r.status == "fail":
            worst = "fail"
        elif r.status == "warn" and worst != "fail":
            worst = "warn"
    code = {"ok": 0, "warn": 1, "fail": 2}[worst]
    return results, code


def format_table(results: list[CheckResult]) -> str:
    icons = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    width = max(len(r.name) for r in results) if results else 16
    lines = []
    for r in results:
        ic = icons.get(r.status, "?")
        lines.append(f"  {ic} {r.name:<{width}}  {r.message}")
    return "\n".join(lines)
