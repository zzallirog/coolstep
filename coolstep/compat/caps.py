"""Platform capability flags — the RNA transcript for this host.

detect.py transcribes (reads the DNA = system state).
Adapters and actuators read PlatformCaps to decide whether to express.
The installer reads PlatformCaps to build an InstallPlan.

This module is pure data: no I/O, no side-effects, no imports from coolstep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

CpuVendor = Literal["amd", "intel", "arm", "unknown"]
GpuStack = Literal["amdgpu", "nvidia", "intel", "none", "unknown"]
Compositor = Literal[
    "hyprland", "sway", "kwin", "mutter", "xfwm4", "openbox",
    "x11_generic", "none", "unknown",
]
DistroClan = Literal["arch", "debian", "fedora", "suse", "nixos", "unknown"]


@dataclass(frozen=True)
class PlatformCaps:
    """Immutable capability snapshot for this host at detection time.

    Fields are grouped by subsystem. Boolean fields are True only when the
    capability is confirmed available and usable (binary on PATH, sysfs
    readable, etc.). A False field means "coolstep will not use this on the
    current host" — not necessarily "permanently broken".
    """

    # --- OS / distro ------------------------------------------------
    distro_id: str               # "arch", "ubuntu", "fedora", …
    distro_name: str             # human name from /etc/os-release
    distro_clan: DistroClan      # family bucket for installer logic
    distro_like: tuple[str, ...] # ID_LIKE from os-release (empty if absent)
    pkg_manager: str             # "pacman", "apt", "dnf", "zypper", "nix", ""

    # --- CPU --------------------------------------------------------
    cpu_vendor: CpuVendor
    cpu_model: str               # model name string from /proc/cpuinfo
    hwmon_k10temp: bool          # AMD k10temp driver in hwmon
    hwmon_coretemp: bool         # Intel coretemp driver in hwmon
    hwmon_zenpower: bool         # zenpower (AMD alt) in hwmon
    hwmon_amd_energy: bool       # amd_energy driver (AMD RAPL equivalent)
    hwmon_rapl: bool             # Intel RAPL powercap at /sys/.../intel-rapl
    rapl_powercap: bool          # /sys/class/powercap/intel-rapl exists (Intel+AMD)
    rapl_readable: bool          # energy_uj actually readable (CVE-2020-8694 gate)
    hwmon_superio: tuple[str, ...]   # detected nct6xxx / it87 / asus-ec etc.
    thermal_zone_count: int      # /sys/class/thermal/thermal_zone* count
    cpu_arch: str                # x86_64 / aarch64 / armv7l / riscv64 / …
    epp_available: bool          # energy_performance_preference is readable
    epp_writable: bool           # …and writable (needed by epp_shift actuator)
    platform_profile_available: bool  # /sys/firmware/acpi/platform_profile
    perf_event_paranoid: int     # /proc/sys/kernel/perf_event_paranoid (-1..3, 99=unknown)

    # --- GPU --------------------------------------------------------
    gpu_primary: GpuStack        # primary render GPU (dGPU wins over iGPU)
    gpu_amd: bool                # AMD GPU detected via drm card vendor
    gpu_nvidia: bool             # NVIDIA GPU detected
    gpu_intel: bool              # Intel GPU (i915 / xe)
    pynvml_importable: bool      # nvidia-ml-py importable in current env

    # --- Compositor / WM --------------------------------------------
    compositor: Compositor
    hyprctl_available: bool      # hyprctl binary on PATH
    swaymsg_available: bool
    wmctrl_available: bool       # X11 wmctrl
    xdotool_available: bool      # X11 xdotool

    # --- Fan control tools ------------------------------------------
    asusctl_available: bool
    asusctl_version: str         # "6.1.2" or "" if absent/old
    asusctl_version_ok: bool     # major >= 6
    nbfc_available: bool
    fancontrol_available: bool   # lm-sensors fancontrol
    thinkfan_available: bool
    dell_smm_hwmon: bool         # /sys/module/dell_smm_hwmon loaded
    coolercontrol_available: bool

    # --- Power tools ------------------------------------------------
    ryzenadj_available: bool
    auto_cpufreq_available: bool
    tlp_available: bool
    tlp_active: bool             # tlp.service running
    power_profiles_daemon_active: bool  # ppd.service active
    nvidia_smi_available: bool

    # --- systemd ----------------------------------------------------
    systemd_user_available: bool
    systemd_unit_user_dir: str   # e.g. ~/.config/systemd/user

    # --- kernel modules ---------------------------------------------
    kmod_amdgpu: bool
    kmod_nvidia: bool
    kmod_i915: bool
    kmod_xe: bool                # new Intel Xe driver (replaces i915 on newer HW)
    kmod_ipmi: bool              # ipmi_si / ipmi_devintf for local KCS
    kmod_dell_smm_hwmon: bool    # already declared above? No: dell_smm_hwmon flag is separate

    # --- enterprise / server -----------------------------------------
    ipmitool_available: bool
    redfish_endpoint_configured: bool  # COOLSTEP_REDFISH_URL set in env
    bpftrace_available: bool
    bcc_importable: bool             # python-bcc importable
    perf_binary_available: bool      # `perf` binary on PATH
    dbus_session_active: bool        # DBUS_SESSION_BUS_ADDRESS in env

    # --- misc -------------------------------------------------------
    secure_boot: bool | None     # None = detection not possible
    is_virtual: bool             # running inside KVM/VMware/Xen/cloud

    # --- detection metadata -----------------------------------------
    detected_at: float = field(default_factory=time.time)
    warnings: tuple[str, ...] = ()

    # ----------------------------------------------------------------
    # Derived helpers (read-only computed properties)

    @property
    def has_fan_control(self) -> bool:
        """At least one fan-control tool is available."""
        return (
            self.asusctl_available
            or self.nbfc_available
            or self.fancontrol_available
            or self.thinkfan_available
            or self.coolercontrol_available
        )

    @property
    def has_wm_context(self) -> bool:
        """Workload context can be collected (knows what app is running)."""
        return (
            self.compositor in ("hyprland", "sway")
            or self.wmctrl_available
            or (self.dbus_session_active and self.compositor in ("kwin", "mutter"))
        )

    @property
    def facet(self) -> str:
        """Deployment target derived from caps — not stored, computed live.

        Returns one of: "desktop" | "server" | "ambiguous".

        - "desktop" — laptop/workstation/NUC focus: fan noise control,
          workload context, EPP/ASUS-style actuators.  Has a battery OR
          a graphical compositor OR consumer-grade platform_profile.

        - "server" — rack / homelab host: BMC-managed fans, RAPL+IPMI
          telemetry, perf+eBPF for foresight.  No battery, no DE, or
          ipmi kmod loaded.

        - "ambiguous" — virtual / cloud / dev container where heuristics
          conflict.  Default to "desktop" packaging unless explicit
          --facet=server.
        """
        # Strong server signals — BMC tooling, ipmi kernel modules
        server_hints = (
            self.kmod_ipmi
            or self.ipmitool_available
            or self.redfish_endpoint_configured
        )
        if server_hints:
            return "server"
        # Cloud / VM without consumer power-management → server
        if self.is_virtual and not self.platform_profile_available:
            return "server"
        # Headless physical host (no compositor, no laptop profile cycle) → server
        # Note: epp_available is NOT a laptop signal — modern Xeon and EPYC also expose EPP.
        if self.compositor == "none" and not self.platform_profile_available:
            return "server"
        # Has a graphical compositor or a laptop profile cycle → desktop
        if (
            self.compositor not in ("none", "unknown")
            or self.platform_profile_available
        ):
            return "desktop"
        return "ambiguous"

    @property
    def collectors_expected(self) -> list[str]:
        """Collectors that should discover on this platform (best-effort).

        Three-segment coverage:
        - Workstation/laptop: linux_sysfs + GPU + WM
        - Home lab / NUC: linux_sysfs + i915/xe + rapl
        - Rack server / cloud: redfish or ipmi + rapl + thermal_zones
        """
        result = ["linux_sysfs", "arm_thermal"]
        # GPU
        if self.gpu_amd:
            result.append("amdgpu")
        if self.gpu_nvidia and self.pynvml_importable:
            result.append("nvidia_nvml")
        if self.gpu_intel:
            result.append("intel_i915")
        # Power / energy
        if self.rapl_powercap and self.rapl_readable:
            result.append("rapl_energy")
        # Workload context
        if self.hyprctl_available:
            result.append("hyprctl")
        elif self.compositor in ("kwin", "mutter") and self.dbus_session_active:
            result.append("dbus_session")
        # Enterprise
        if self.redfish_endpoint_configured:
            result.append("redfish")
        if self.ipmitool_available:
            result.append("ipmi")
        # Future
        if self.perf_binary_available and self.perf_event_paranoid <= 2:
            result.append("perf_events")
        if self.bpftrace_available or self.bcc_importable:
            result.append("ebpf_sched")
        return result

    @property
    def actuators_expected(self) -> list[str]:
        """Actuators that should discover (excluding readonly_log)."""
        result = []
        if self.asusctl_available and self.asusctl_version_ok:
            result.append("asusctl_fan_curve_bias")
        if self.epp_writable:
            result.append("epp_shift")
        if self.ryzenadj_available:
            result.append("ryzenadj_cap")
        return result
