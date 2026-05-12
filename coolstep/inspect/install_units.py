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

# Cleanup script is optional (`-` prefix ignores exit code on missing file).
# AUR builds ship the script at /usr/lib/coolstep/cleanup.sh; this template
# falls back to the maintainer dev location.  Python's `try/finally` in
# daemon.run() is the primary revert path; this is the SIGKILL/OOM belt.
ExecStopPost=-%h/.local/share/coolstep/cleanup.sh
ExecStopPost=-/usr/lib/coolstep/cleanup.sh

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


def install(force: bool = False) -> list[tuple[str, str]]:
    """Write systemd unit files for the current user.

    Returns a list of (unit_name, status) tuples.  Status is one of:
    `written`, `skipped (exists)`, `replaced`.
    """
    target = _unit_dir()
    target.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for name, content in UNITS.items():
        path = target / name
        if path.exists() and not force:
            results.append((name, "skipped (exists; use --force)"))
            continue
        status = "replaced" if path.exists() else "written"
        path.write_text(content, encoding="utf-8")
        results.append((name, status))
    return results
