"""DBus session workload context — KDE Plasma / GNOME Shell / generic XDG.

Real implementation via `gdbus` subprocess.  Why gdbus (not dasbus / pydbus):
- `gdbus` ships with GLib → on every KDE/GNOME desktop by default.
- Zero Python dependency → no pip install in install_plan.
- "Compose, not replace" — use the tool the desktop already provides.

Two queries per sample:
1. **Focused app** — DE-specific endpoint, first matching wins:
   - KDE Plasma (KWin): `org.kde.KWin.activeWindow` → win id → resourceClass
   - GNOME Shell:       `org.gnome.Shell.Eval` running-app heuristic
2. **Idle hint** — universal `org.freedesktop.ScreenSaver.GetSessionIdleTime`

Cached for 1.0 s — focused window rarely changes faster than that.

Output: WorkloadFrame.label and platform_state["dbus.idle_active"].
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, ProcSig, SignalDescriptor, WorkloadFrame

log = logging.getLogger(__name__)

_CACHE_SEC = 1.0
_SUBPROC_TIMEOUT = 1.0

# gdbus parses output like:
#   (uint32 4842,)
#   ('Firefox',)
# We just need the inner string/integer literal.
_RESOURCE_CLASS_RE = re.compile(r"'([^']+)'")
_UINT32_RE = re.compile(r"uint32\s+(\d+)")
_INT_RE = re.compile(r"-?\d+")


def _gdbus_call(
    dest: str, object_path: str, method: str, *args: str,
) -> str | None:
    """Run `gdbus call --session ...` and return raw stdout.  None on failure."""
    cmd = [
        "gdbus", "call", "--session",
        "--dest", dest,
        "--object-path", object_path,
        "--method", method,
        *args,
    ]
    try:
        r = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _query_kde_active() -> str | None:
    """Resolve KDE Plasma focused window resource class (e.g. 'firefox')."""
    raw = _gdbus_call(
        "org.kde.KWin", "/KWin", "org.kde.KWin.activeWindow",
    )
    if raw is None:
        return None
    m = _UINT32_RE.search(raw) or _INT_RE.search(raw)
    if not m:
        return None
    win_id = m.group(1) if m.lastindex else m.group(0)
    # Try to fetch resourceClass — newer Plasma uses /windows/<id>/...
    props = _gdbus_call(
        "org.kde.KWin",
        f"/windows/{win_id}",
        "org.freedesktop.DBus.Properties.Get",
        "org.kde.KWin.Window",
        "resourceClass",
    )
    if props is None:
        return None
    m = _RESOURCE_CLASS_RE.search(props)
    return m.group(1) if m else None


def _query_gnome_focused() -> str | None:
    """Resolve GNOME Shell focused app via org.gnome.Shell.Introspect.

    Introspect.GetRunningApplications works on GNOME 3.34+ without
    unsafe-mode. An older fallback via Shell.Eval was considered but
    Eval has been restricted by default since GNOME 3.36 and requires
    a per-session opt-in; we don't try it here. If Introspect refuses
    (older GNOME or restricted bus), the function returns None and
    the collector falls back to the universal idle hint only.
    """
    raw = _gdbus_call(
        "org.gnome.Shell",
        "/org/gnome/Shell/Introspect",
        "org.gnome.Shell.Introspect.GetRunningApplications",
    )
    if raw is None:
        return None
    # Output is a complex dict; just grab the first quoted app-id
    m = _RESOURCE_CLASS_RE.search(raw)
    return m.group(1) if m else None


def _query_idle_time_ms() -> int | None:
    """Universal idle hint via XDG ScreenSaver."""
    raw = _gdbus_call(
        "org.freedesktop.ScreenSaver",
        "/org/freedesktop/ScreenSaver",
        "org.freedesktop.ScreenSaver.GetSessionIdleTime",
    )
    if raw is None:
        return None
    m = _UINT32_RE.search(raw) or _INT_RE.search(raw)
    if not m:
        return None
    try:
        return int(m.group(1) if m.lastindex else m.group(0))
    except ValueError:
        return None


class DbusSessionCollector:
    name = "dbus_session"

    def __init__(self) -> None:
        self._cost = CostTracker()
        self._cached_label: str | None = None
        self._cached_idle_ms: int | None = None
        self._cached_at: float = 0.0
        self._has_gdbus = False
        self._dbus_addr = ""
        self._compositor: str = "unknown"

    # ------------------------------------------------------------------
    def discover(self) -> bool:
        self._dbus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        self._has_gdbus = bool(shutil.which("gdbus"))
        if not (self._dbus_addr and self._has_gdbus):
            return False

        # Detect compositor for choosing query path
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            # Hyprland already handled by hyprctl collector; skip.
            return False
        xdg = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
        if "KDE" in xdg or os.environ.get("KDE_FULL_SESSION"):
            self._compositor = "kde"
        elif "GNOME" in xdg or os.environ.get("GNOME_DESKTOP_SESSION_ID"):
            self._compositor = "gnome"
        else:
            self._compositor = "xdg"

        # Probe bus reachability with a universally-available method.
        # ListNames always succeeds on a session bus and returns a list of
        # well-known names; GetId is also universal but less defensive.
        probe = _gdbus_call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.ListNames",
        )
        return probe is not None

    # ------------------------------------------------------------------
    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="workload.focused_app", unit="-", dtype="str",
                source="gdbus org.kde.KWin / org.gnome.Shell.Introspect",
                cardinality="scalar",
                description="Focused window app id (KDE/GNOME via DBus)",
                requires=("gdbus binary", "session DBus"),
            ),
            SignalDescriptor(
                name="workload.idle_ms", unit="ms", dtype="int",
                source="org.freedesktop.ScreenSaver.GetSessionIdleTime",
                cardinality="scalar",
                description="Session idle time (DE-reported)",
                requires=("session DBus",),
            ),
        ]

    # ------------------------------------------------------------------
    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            now = time.monotonic()
            if now - self._cached_at < _CACHE_SEC and self._cached_label is not None:
                # Stale within cache window — return previous
                partial = self._render(self._cached_label, self._cached_idle_ms)
                self._cost.record_us(sw.elapsed_us)
                return partial

            label: str | None = None
            if self._compositor == "kde":
                label = _query_kde_active()
            elif self._compositor == "gnome":
                label = _query_gnome_focused()

            idle_ms = _query_idle_time_ms()
            self._cached_label = label
            self._cached_idle_ms = idle_ms
            self._cached_at = now

            partial = self._render(label, idle_ms)
            self._cost.record_us(sw.elapsed_us)
            return partial

    def _render(self, label: str | None, idle_ms: int | None) -> dict[str, object]:
        partial: dict[str, object] = {}
        if label:
            partial["workload"] = WorkloadFrame(
                top_processes=[ProcSig(pid=0, name=label, cpu_pct=0.0, rss_mb=0.0)],
                rolling_features={"visible_apps": 1.0},
                label=label,
            )
        if idle_ms is not None:
            ps = {"dbus.idle_ms": str(idle_ms)}
            partial["platform_state"] = ps
        return partial


def make() -> DbusSessionCollector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    # Skip on Hyprland — hyprctl is the canonical source there.
    if _c is not None:
        if _c.hyprctl_available and _c.compositor == "hyprland":
            return None
        if not _c.dbus_session_active:
            return None
    collector = DbusSessionCollector()
    return collector if collector.discover() else None
