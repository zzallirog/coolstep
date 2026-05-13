"""Install systemd user units for coolstep.

For pipx / pip --user installs, the units don't get dropped into
`~/.config/systemd/user/` automatically.  This command writes them
from embedded constants, so users who never cloned the repo can still
enable the daemon.

The unit text is kept in sync with `systemd/coolstep-*.service` in
the repo; AUR builds copy from those files directly.  This module is
the fallback path for non-AUR installs.
"""

from __future__ import annotations

import os
import shutil
import stat
from importlib import resources
from pathlib import Path

_COLLECTOR_UNIT = """\
[Unit]
Description=coolstep collector daemon — predictive soft-cooling telemetry
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
# PATH covers pipx (~/.local/bin shim), pip --user, uv tool, AUR (/usr/bin).
Environment=PATH=%h/.local/bin:%h/.local/share/pipx/venvs/coolstep/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env coolstep-collector --period 1.0 --log-level INFO
Restart=on-failure
RestartSec=5

MemoryMax=512M
CPUQuota=10%
Nice=10
IOSchedulingClass=idle

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/coolstep/data
PrivateTmp=true

Environment=COOLSTEP_HOME=%h/coolstep/data
Environment=PYTHONUNBUFFERED=1

# Cleanup script — install() copies it from the wheel into
# `~/.local/share/coolstep/cleanup.sh` so this path resolves on pipx/pip
# installs.  `-` prefix ignores exit code; Python's try/finally in
# daemon.run() is the primary revert path, this is the SIGKILL/OOM belt.
ExecStopPost=-%h/.local/share/coolstep/cleanup.sh

[Install]
WantedBy=default.target
"""

_DASHBOARD_UNIT = """\
[Unit]
Description=coolstep dashboard — FastAPI + SSE
After=coolstep-collector.service
Wants=coolstep-collector.service

[Service]
Type=simple
Environment=PATH=%h/.local/bin:%h/.local/share/pipx/venvs/coolstep/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env coolstep-dashboard --host 127.0.0.1 --port 18889
Restart=on-failure
RestartSec=5

MemoryMax=768M
CPUQuota=30%
Nice=10

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/coolstep/data

Environment=COOLSTEP_HOME=%h/coolstep/data
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""

UNITS = {
    "coolstep-collector.service": _COLLECTOR_UNIT,
    "coolstep-dashboard.service": _DASHBOARD_UNIT,
}


def _unit_dir() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(raw) if raw else (Path.home() / ".config")
    return base / "systemd" / "user"


def _data_dir() -> Path:
    # Matches ReadWritePaths= in the unit templates above.  Systemd 226/NAMESPACE
    # failure if this doesn't exist at start time.
    return Path.home() / "coolstep" / "data"


def _cleanup_target() -> Path:
    # Matches `ExecStopPost=-%h/.local/share/coolstep/cleanup.sh` in the
    # collector unit template.
    return Path.home() / ".local" / "share" / "coolstep" / "cleanup.sh"


def _install_cleanup_script() -> tuple[str, str]:
    """Copy bundled cleanup.sh out of the wheel onto disk, chmod 0755.

    Returns (target, status) where status is `written`, `replaced`, or
    `missing (wheel resource)`.  The unit's `-` prefix on ExecStopPost
    tolerates a missing file, so a failure here is non-fatal.
    """
    target = _cleanup_target()
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        src = resources.files("coolstep._resources").joinpath("cleanup.sh")
        with resources.as_file(src) as path:
            existed = target.exists()
            shutil.copyfile(path, target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return (str(target), "replaced" if existed else "written")
    except (FileNotFoundError, ModuleNotFoundError):
        return (str(target), "missing (wheel resource)")


def install(force: bool = False) -> list[tuple[str, str]]:
    """Write systemd unit files for the current user.

    Also creates the runtime data directory (~/coolstep/data) that the
    units reference in `ReadWritePaths=`; without it, systemd refuses to
    set up the mount namespace and the daemon dies with status 226/NAMESPACE
    in an infinite restart loop.

    Returns a list of (target, status) tuples.  Status is one of:
    `written`, `skipped (exists)`, `replaced`, `created`.
    """
    results: list[tuple[str, str]] = []

    data = _data_dir()
    if data.exists():
        results.append((str(data), "skipped (exists)"))
    else:
        data.mkdir(parents=True, exist_ok=True)
        results.append((str(data), "created"))

    results.append(_install_cleanup_script())

    target = _unit_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name, content in UNITS.items():
        path = target / name
        if path.exists() and not force:
            results.append((name, "skipped (exists; use --force)"))
            continue
        status = "replaced" if path.exists() else "written"
        path.write_text(content, encoding="utf-8")
        results.append((name, status))
    return results
