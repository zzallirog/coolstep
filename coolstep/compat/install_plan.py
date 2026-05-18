"""Install plan — translates PlatformCaps into actionable installer output.

Usage:
    from coolstep.compat import get_caps
    from coolstep.compat.install_plan import caps_to_install_plan

    plan = caps_to_install_plan(get_caps())
    print(plan.summary())        # human-readable
    print(plan.to_json())        # for installer script consumption
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from coolstep.compat.caps import PlatformCaps

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-distro package name mappings for coolstep dependencies
# Key: logical name; value: dict[clan -> package_name_or_empty]
# Empty string means "not available via this distro's package manager"
# ---------------------------------------------------------------------------
_PKG: dict[str, dict[str, str]] = {
    # Python
    "python3": {
        "arch": "python",
        "debian": "python3",
        "fedora": "python3",
        "suse": "python3",
        "nixos": "python3",
    },
    "python3-pip": {
        "arch": "python-pip",
        "debian": "python3-pip",
        "fedora": "python3-pip",
        "suse": "python3-pip",
        "nixos": "",
    },
    # System monitoring
    "lm-sensors": {
        "arch": "lm_sensors",
        "debian": "lm-sensors",
        "fedora": "lm_sensors",
        "suse": "sensors",
        "nixos": "lm_sensors",
    },
    # Power management
    "power-profiles-daemon": {
        "arch": "power-profiles-daemon",
        "debian": "power-profiles-daemon",
        "fedora": "power-profiles-daemon",
        "suse": "power-profiles-daemon",
        "nixos": "power-profiles-daemon",
    },
    "tlp": {
        "arch": "tlp",
        "debian": "tlp",
        "fedora": "tlp",
        "suse": "tlp",
        "nixos": "tlp",
    },
    # Fan control
    "asusctl": {
        "arch": "asusctl",         # AUR
        "debian": "",              # not packaged
        "fedora": "",
        "suse": "",
        "nixos": "asusctl",
    },
    "nbfc-linux": {
        "arch": "nbfc-linux",      # AUR
        "debian": "",
        "fedora": "",
        "suse": "",
        "nixos": "",
    },
    "thinkfan": {
        "arch": "thinkfan",
        "debian": "thinkfan",
        "fedora": "thinkfan",
        "suse": "thinkfan",
        "nixos": "thinkfan",
    },
    # AMD power
    "ryzenadj": {
        "arch": "ryzenadj",        # AUR
        "debian": "",              # build from source
        "fedora": "",
        "suse": "",
        "nixos": "ryzenadj",
    },
    # NVIDIA
    "nvidia-utils": {
        "arch": "nvidia-utils",
        "debian": "nvidia-utils",
        "fedora": "akmod-nvidia",
        "suse": "nvidia-glG06",
        "nixos": "",
    },
    # Server / enterprise
    "ipmitool": {
        "arch": "ipmitool",
        "debian": "ipmitool",
        "fedora": "ipmitool",
        "suse": "ipmitool",
        "nixos": "ipmitool",
    },
    "freeipmi-tools": {
        "arch": "freeipmi",
        "debian": "freeipmi-tools",
        "fedora": "freeipmi",
        "suse": "freeipmi",
        "nixos": "freeipmi",
    },
    # eBPF / tracing
    "bpftrace": {
        "arch": "bpftrace",
        "debian": "bpftrace",
        "fedora": "bpftrace",
        "suse": "bpftrace",
        "nixos": "bpftrace",
    },
    "python-bcc": {
        "arch": "python-bcc",
        "debian": "python3-bpfcc",
        "fedora": "python3-bcc",
        "suse": "python3-bcc",
        "nixos": "bcc",
    },
    # perf binary (kernel-tools / linux-tools)
    "perf": {
        "arch": "perf",
        "debian": "linux-tools-generic",
        "fedora": "perf",
        "suse": "perf",
        "nixos": "linuxPackages.perf",
    },
    # DBus session helpers
    "dbus": {
        "arch": "dbus",
        "debian": "dbus",
        "fedora": "dbus-daemon",
        "suse": "dbus-1",
        "nixos": "dbus",
    },
    # Intel GPU userspace utilities (intel_gpu_top for verification)
    "intel-gpu-tools": {
        "arch": "intel-gpu-tools",
        "debian": "intel-gpu-tools",
        "fedora": "igt-gpu-tools",
        "suse": "intel-gpu-tools",
        "nixos": "intel-gpu-tools",
    },
}

# Python packages installed via pip (distro-agnostic)
_PIP_PKGS: dict[str, str] = {
    "fastapi": "fastapi[standard]",
    "uvicorn": "uvicorn",
    "click": "click",
    "pynvml": "nvidia-ml-py",
    "chromadb": "chromadb",
}


def _pkg(name: str, clan: str) -> str:
    """Return distro package name, or '' if unavailable."""
    return _PKG.get(name, {}).get(clan, "")


@dataclass
class CollectorStatus:
    name: str
    will_activate: bool
    reason: str = ""  # why not, if will_activate is False


@dataclass
class ActuatorStatus:
    name: str
    will_activate: bool
    reason: str = ""


@dataclass
class CommunityPointer:
    """A hint to the user: missing community-maintained driver/tool.

    Coolstep does NOT write hardware drivers — it composes with what the
    Linux community already maintains.  When detection notices hardware
    coolstep could read but lacks the driver for, we emit a pointer with
    the distro-specific install command.

    Example:
        CommunityPointer(
            trigger="MSI motherboard detected, nct6687 driver missing",
            reason="Without nct6687 kernel driver, fan RPMs are invisible",
            commands={
                "arch":   "yay -S nct6687-driver-dkms-git",
                "debian": "apt install nct6687-dkms  # may need backports",
                "fedora": "akmod-nct6687  # from rpmfusion",
            },
            url="https://github.com/Fred78290/nct6687d",
        )
    """

    trigger: str          # what we detected
    reason: str           # why this matters for coolstep
    commands: dict[str, str]   # clan -> install command
    url: str = ""         # upstream repo / docs


@dataclass
class InstallPlan:
    """Structured install recommendation derived from PlatformCaps."""

    distro_id: str
    distro_clan: str
    pkg_manager: str

    # Packages
    packages_required: list[str] = field(default_factory=list)
    packages_optional: list[str] = field(default_factory=list)
    packages_aur: list[str] = field(default_factory=list)   # Arch AUR
    pip_required: list[str] = field(default_factory=list)
    pip_optional: list[str] = field(default_factory=list)

    # Adapter status
    collectors: list[CollectorStatus] = field(default_factory=list)
    actuators: list[ActuatorStatus] = field(default_factory=list)

    # systemd
    systemd_units_to_enable: list[str] = field(default_factory=list)
    env_suggestions: dict[str, str] = field(default_factory=dict)

    # Misc
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    community_pointers: list[CommunityPointer] = field(default_factory=list)
    facet: str = "ambiguous"   # "desktop" | "server" | "ambiguous"

    def summary(self) -> str:
        lines: list[str] = [
            f"=== coolstep install plan ({self.distro_id} / {self.distro_clan}) ===",
            "",
        ]

        if self.packages_required:
            cmd = _install_cmd(self.pkg_manager, self.packages_required)
            lines += ["Required packages:", f"  {cmd}", ""]
        if self.packages_optional:
            cmd = _install_cmd(self.pkg_manager, self.packages_optional)
            lines += ["Optional packages:", f"  {cmd}", ""]
        if self.packages_aur:
            lines += [
                "AUR packages (use yay/paru):",
                f"  yay -S {' '.join(self.packages_aur)}",
                "",
            ]
        if self.pip_required:
            lines += [
                "pip (required):",
                f"  pip install {' '.join(self.pip_required)}",
                "",
            ]
        if self.pip_optional:
            lines += [
                "pip (optional):",
                f"  pip install {' '.join(self.pip_optional)}",
                "",
            ]

        lines.append("Collectors:")
        for c in self.collectors:
            mark = "✓" if c.will_activate else "✗"
            suffix = f"  ({c.reason})" if c.reason else ""
            lines.append(f"  {mark} {c.name}{suffix}")

        lines.append("")
        lines.append("Actuators:")
        for a in self.actuators:
            mark = "✓" if a.will_activate else "✗"
            suffix = f"  ({a.reason})" if a.reason else ""
            lines.append(f"  {mark} {a.name}{suffix}")

        if self.systemd_units_to_enable:
            lines += [
                "",
                "Enable systemd units:",
                *[f"  systemctl --user enable --now {u}" for u in self.systemd_units_to_enable],
            ]

        if self.env_suggestions:
            lines += ["", "Suggested env vars:"]
            for k, v in self.env_suggestions.items():
                lines.append(f"  {k}={v}")

        if self.warnings:
            lines += ["", "Warnings:"]
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")

        if self.notes:
            lines += ["", "Notes:"]
            for n in self.notes:
                lines.append(f"  • {n}")

        if self.community_pointers:
            lines += ["", f"Community drivers (your '{self.facet}' target needs these):"]
            for p in self.community_pointers:
                lines.append(f"  ◆ {p.trigger}")
                lines.append(f"      reason: {p.reason}")
                cmd = p.commands.get(self.distro_clan) or next(iter(p.commands.values()), "")
                if cmd:
                    lines.append(f"      run:    {cmd}")
                if p.url:
                    lines.append(f"      upstream: {p.url}")

        lines += ["", f"Deployment target: {self.facet}"]

        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _install_cmd(pkg_manager: str, packages: list[str]) -> str:
    cmds = {
        "pacman": f"sudo pacman -S {' '.join(packages)}",
        "apt": f"sudo apt install {' '.join(packages)}",
        "dnf": f"sudo dnf install {' '.join(packages)}",
        "zypper": f"sudo zypper install {' '.join(packages)}",
        "nix": f"nix-env -iA nixpkgs.{' nixpkgs.'.join(packages)}",
    }
    return cmds.get(pkg_manager, f"install: {' '.join(packages)}")


def caps_to_install_plan(
    caps: PlatformCaps,
    facet_override: str | None = None,
) -> InstallPlan:  # noqa: C901 (complexity ok, explicit > DRY)
    """Build an InstallPlan from PlatformCaps.

    `facet_override` forces a packaging preset:
        "desktop"  — laptop/workstation/NUC tuning
        "server"   — rack/homelab BMC tuning
        None       — auto-detect via caps.facet
    """
    clan = caps.distro_clan
    pm = caps.pkg_manager
    facet = facet_override or caps.facet

    plan = InstallPlan(
        distro_id=caps.distro_id,
        distro_clan=clan,
        pkg_manager=pm,
        warnings=list(caps.warnings),
        facet=facet,
    )

    # --- Base Python deps -------------------------------------------
    # Always needed; assume they're present if we're running, but list
    # them for a fresh install scenario.
    plan.packages_required.extend(
        x for x in [_pkg("python3", clan), _pkg("python3-pip", clan)] if x
    )
    plan.pip_required.extend([
        _PIP_PKGS["fastapi"],
        _PIP_PKGS["uvicorn"],
        _PIP_PKGS["click"],
        _PIP_PKGS["chromadb"],
    ])

    # --- Sensors (always useful) ------------------------------------
    sensors_pkg = _pkg("lm-sensors", clan)
    if sensors_pkg:
        plan.packages_optional.append(sensors_pkg)

    # --- GPU: NVIDIA ------------------------------------------------
    if caps.gpu_nvidia:
        nvidia_pkg = _pkg("nvidia-utils", clan)
        if nvidia_pkg:
            plan.packages_optional.append(nvidia_pkg)
        if not caps.pynvml_importable:
            plan.pip_required.append(_PIP_PKGS["pynvml"])
            plan.warnings.append(
                "nvidia-ml-py not importable — install via pip to enable nvidia_nvml collector"
            )

    # --- Intel iGPU (i915 / xe) ------------------------------------
    if caps.gpu_intel:
        igt = _pkg("intel-gpu-tools", clan)
        if igt:
            plan.packages_optional.append(igt)

    # --- Server segment: IPMI / Redfish ----------------------------
    # Heuristic: virtual machine OR no battery (server-like) OR ipmi kmod loaded
    server_like = caps.is_virtual or caps.kmod_ipmi or not caps.platform_profile_available
    if server_like:
        if not caps.ipmitool_available:
            ipmi_pkg = _pkg("ipmitool", clan)
            if ipmi_pkg:
                plan.packages_optional.append(ipmi_pkg)
        if not caps.redfish_endpoint_configured:
            plan.notes.append(
                "Server-class host detected — for BMC monitoring set "
                "COOLSTEP_REDFISH_URL / _USER / _PASS env vars"
            )

    # --- eBPF / perf observability ---------------------------------
    if not caps.perf_binary_available:
        perf_pkg = _pkg("perf", clan)
        if perf_pkg:
            plan.packages_optional.append(perf_pkg)
            plan.notes.append("perf binary recommended for PMU counters (pre-thermal load signal)")

    if not (caps.bpftrace_available or caps.bcc_importable):
        bpftrace_pkg = _pkg("bpftrace", clan)
        if bpftrace_pkg:
            plan.packages_optional.append(bpftrace_pkg)
        bcc_pkg = _pkg("python-bcc", clan)
        if bcc_pkg:
            plan.packages_optional.append(bcc_pkg)

    # --- RAPL readability warning ----------------------------------
    if caps.rapl_powercap and not caps.rapl_readable:
        plan.notes.append(
            "RAPL counters present but root-only.  Either run coolstep as root, "
            "or `sudo setcap cap_sys_admin+ep /usr/bin/python3.NN` (less secure), "
            "or use AmbientCapabilities=CAP_SYS_ADMIN in the systemd unit."
        )

    # --- Fan control ------------------------------------------------
    if caps.gpu_amd or caps.hwmon_k10temp:
        # ASUS laptops → asusctl
        asusctl_pkg = _pkg("asusctl", clan)
        if not caps.asusctl_available:
            if clan == "arch":
                plan.packages_aur.append(asusctl_pkg)
            elif asusctl_pkg:
                plan.packages_optional.append(asusctl_pkg)
            else:
                plan.notes.append(
                    "asusctl not available for this distro — "
                    "build from https://gitlab.com/asus-linux/asusctl"
                )

    if not caps.has_fan_control:
        thinkfan_pkg = _pkg("thinkfan", clan)
        if thinkfan_pkg:
            plan.packages_optional.append(thinkfan_pkg)
        if clan == "arch":
            plan.packages_aur.append("nbfc-linux")

    # --- Power tools ------------------------------------------------
    if caps.cpu_vendor == "amd" and not caps.ryzenadj_available:
        ryzenadj_pkg = _pkg("ryzenadj", clan)
        if clan == "arch":
            plan.packages_aur.append("ryzenadj")
        elif ryzenadj_pkg:
            plan.packages_optional.append(ryzenadj_pkg)
        else:
            plan.notes.append(
                "ryzenadj not packaged for this distro — "
                "build from https://github.com/FlyGoat/RyzenAdj"
            )

    if not caps.power_profiles_daemon_active and not caps.tlp_active:
        ppd_pkg = _pkg("power-profiles-daemon", clan)
        if ppd_pkg:
            plan.packages_optional.append(ppd_pkg)
            plan.notes.append(
                "No power manager active — power-profiles-daemon recommended "
                "(conflicts with TLP)"
            )

    # --- Collector status -------------------------------------------
    plan.collectors.append(CollectorStatus("linux_sysfs", True))
    plan.collectors.append(CollectorStatus(
        "arm_thermal",
        caps.thermal_zone_count > 0,
        "" if caps.thermal_zone_count > 0
        else "no /sys/class/thermal/thermal_zone* zones (ACPI thermal disabled)",
    ))

    plan.collectors.append(CollectorStatus(
        "amdgpu",
        caps.gpu_amd,
        "" if caps.gpu_amd else "no AMD GPU detected (drm vendor 0x1002 / kmod amdgpu)",
    ))

    plan.collectors.append(CollectorStatus(
        "nvidia_nvml",
        caps.gpu_nvidia and caps.pynvml_importable,
        "" if (caps.gpu_nvidia and caps.pynvml_importable)
        else (
            "no NVIDIA GPU" if not caps.gpu_nvidia
            else "pynvml not importable (pip install nvidia-ml-py)"
        ),
    ))

    plan.collectors.append(CollectorStatus(
        "intel_i915",
        caps.gpu_intel,
        "" if caps.gpu_intel else "no Intel GPU (drm vendor 0x8086 / kmod i915 or xe)",
    ))

    plan.collectors.append(CollectorStatus(
        "rapl_energy",
        caps.rapl_powercap and caps.rapl_readable,
        "" if (caps.rapl_powercap and caps.rapl_readable)
        else (
            "RAPL counters present but unreadable (CVE-2020-8694: needs root/CAP_SYS_ADMIN)"
            if caps.rapl_powercap
            else "no /sys/class/powercap/intel-rapl"
        ),
    ))

    plan.collectors.append(CollectorStatus(
        "hyprctl",
        caps.hyprctl_available,
        "" if caps.hyprctl_available else "hyprctl binary not found — Hyprland not installed",
    ))

    plan.collectors.append(CollectorStatus(
        "dbus_session",
        caps.compositor in ("kwin", "mutter") and caps.dbus_session_active,
        "" if (caps.compositor in ("kwin", "mutter") and caps.dbus_session_active)
        else (
            "DBus session bus not active" if not caps.dbus_session_active
            else f"compositor {caps.compositor} — DBus collector only for kwin/mutter"
        ),
    ))

    plan.collectors.append(CollectorStatus(
        "redfish",
        caps.redfish_endpoint_configured,
        "" if caps.redfish_endpoint_configured
        else "no COOLSTEP_REDFISH_URL env (set for rack-server BMC)",
    ))

    plan.collectors.append(CollectorStatus(
        "ipmi",
        caps.ipmitool_available,
        "" if caps.ipmitool_available else "ipmitool not installed (apt/dnf install ipmitool)",
    ))

    plan.collectors.append(CollectorStatus(
        "perf_events",
        caps.perf_binary_available and caps.perf_event_paranoid <= 2,
        "" if (caps.perf_binary_available and caps.perf_event_paranoid <= 2)
        else (
            f"perf_event_paranoid={caps.perf_event_paranoid} > 2 (lock down)"
            if caps.perf_binary_available
            else "perf binary missing (install linux-tools-common / perf)"
        ),
    ))

    plan.collectors.append(CollectorStatus(
        "ebpf_sched",
        caps.bpftrace_available or caps.bcc_importable,
        "" if (caps.bpftrace_available or caps.bcc_importable)
        else "neither bpftrace nor bcc installed — stub-only",
    ))

    # --- Actuator status --------------------------------------------
    plan.actuators.append(CollectorStatus("readonly_log", True))  # always active

    asusctl_active = caps.asusctl_available and caps.asusctl_version_ok
    plan.actuators.append(ActuatorStatus(
        "asusctl_fan_curve_bias",
        asusctl_active,
        "" if asusctl_active
        else (
            f"asusctl found but v{caps.asusctl_version} < 6 (upgrade required)"
            if caps.asusctl_available
            else "asusctl not found"
        ),
    ))

    plan.actuators.append(ActuatorStatus(
        "epp_shift",
        caps.epp_writable,
        "" if caps.epp_writable
        else (
            "EPP readable but not writable (needs write permission or root)"
            if caps.epp_available
            else "energy_performance_preference not available (kernel/platform unsupported)"
        ),
    ))

    plan.actuators.append(ActuatorStatus(
        "ryzenadj_cap",
        caps.ryzenadj_available,
        "" if caps.ryzenadj_available else "ryzenadj not found",
    ))

    # --- systemd ----------------------------------------------------
    plan.systemd_units_to_enable += [
        "coolstep-collector.service",
        "coolstep-dashboard.service",
    ]

    # --- Env suggestions --------------------------------------------
    if caps.compositor == "hyprland":
        plan.env_suggestions["COOLSTEP_ACTUATOR_ENABLE"] = "false"
        plan.notes.append(
            "Start with COOLSTEP_ACTUATOR_ENABLE=false (dry-run) "
            "until calibration completes"
        )

    if caps.gpu_nvidia and caps.pynvml_importable:
        plan.env_suggestions["COOLSTEP_NVIDIA_ENABLE"] = "1"

    # --- Community pointers ----------------------------------------
    plan.community_pointers.extend(_build_community_pointers(caps))

    # --- Facet-specific env / notes --------------------------------
    if facet == "server":
        plan.env_suggestions["COOLSTEP_ACTUATOR_ENABLE"] = "false"
        plan.env_suggestions["COOLSTEP_FACET"] = "server"
        plan.notes.append(
            "Server target: actuators stay disabled — BMC owns chassis fans. "
            "coolstep is your foresight panel, not your fan controller."
        )
        if caps.rapl_powercap and not caps.rapl_readable:
            plan.notes.append(
                "For perf/RAPL on server kernels, prefer AmbientCapabilities=CAP_SYS_ADMIN "
                "in the systemd unit over running as root."
            )
    elif facet == "desktop":
        plan.env_suggestions["COOLSTEP_FACET"] = "desktop"
        plan.notes.append(
            "Desktop target: dashboard first, actuator only after 24h calibration + 3 throttle events."
        )

    return plan


# ---------------------------------------------------------------------------
# Community pointers — "compose, not replace" hints
# ---------------------------------------------------------------------------

def _pointer_from_dict(d: dict) -> CommunityPointer:
    """Build CommunityPointer from a manifest entry."""
    return CommunityPointer(
        trigger=d.get("trigger", ""),
        reason=d.get("reason", ""),
        commands=dict(d.get("commands", {})),
        url=d.get("url", ""),
    )


def _load_pointers_by_id() -> dict[str, CommunityPointer]:
    """Fetch all community pointers from the layered manifest, keyed by id."""
    from coolstep.compat.manifest import load_manifest
    raw = load_manifest().get("community_pointers", [])
    out: dict[str, CommunityPointer] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        if not pid:
            continue
        out[pid] = _pointer_from_dict(entry)
    return out


# Pointer-id → activation condition.  Each predicate inspects caps and returns
# True if the pointer should be surfaced to the user.  Keeping this map small
# and explicit (rather than embedding rule DSL in JSON) — predicates need
# Python logic that JSON would obscure.
def _pointer_conditions(caps: PlatformCaps) -> dict[str, bool]:
    is_asus_hardware = "asus" in caps.cpu_model.lower() or any(
        "asus" in n.lower() for n in caps.hwmon_superio
    )
    is_workstation = (
        not caps.hwmon_superio
        and caps.cpu_vendor in ("amd", "intel")
        and not caps.asusctl_available
        and not caps.dell_smm_hwmon
        and not caps.platform_profile_available
    )
    return {
        "asusctl":         caps.facet == "desktop" and is_asus_hardware and not caps.asusctl_available,
        "ryzenadj":        caps.facet == "desktop" and caps.cpu_vendor == "amd" and not caps.ryzenadj_available,
        "nct6687":         caps.facet == "desktop" and is_workstation,
        "perf_paranoid":   caps.perf_event_paranoid > 2 and caps.perf_binary_available,
        "rapl_perm":       caps.rapl_powercap and not caps.rapl_readable,
        "bpftrace":        not (caps.bpftrace_available or caps.bcc_importable),
        "ipmitool_server": caps.facet == "server" and not caps.ipmitool_available,
    }


def _build_community_pointers(caps: PlatformCaps) -> list[CommunityPointer]:
    """Translate detected gaps into actionable install commands.

    Pointer content comes from the layered manifest (L0/L1/L2 deep-merged).
    Activation logic stays in Python — predicates need facet + hardware
    introspection that's awkward in pure JSON rules.
    """
    available = _load_pointers_by_id()
    conditions = _pointer_conditions(caps)
    out: list[CommunityPointer] = []
    for pid, active in conditions.items():
        if not active:
            continue
        pointer = available.get(pid)
        if pointer is None:
            log.debug("manifest missing community pointer id=%r — skipping", pid)
            continue
        out.append(pointer)
    return out
