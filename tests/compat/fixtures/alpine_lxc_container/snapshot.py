"""Alpine Linux 3.19 musl inside an LXC container — absolute degenerate case.

Minimal /sys: no thermal_zones, no hwmon, no powercap, no DRM, no DMI.
No EPP (LXC guests see the host cpufreq but it's typically not writable).
cpu_vendor determined from /proc/cpuinfo vendor_id (host CPU leaks through).
is_virtual=True via DMI: sys_vendor likely empty (LXC has no DMI at all) →
dmi_hints won't match, so is_virtual=False. We use an explicit "LXC" dmi
hint ... but "lxc" is not in the manifest's dmi_hints. So is_virtual=False
for a bare LXC. That's correct — is_virtual detects KVM/cloud, not LXC.

Tests that detect_caps() survives near-empty /sys without any crash or
exception, returns a coherent PlatformCaps, and facet="server" (no
compositor, no platform_profile, not is_virtual but also no BMC tools —
falls to "compositor==none and no platform_profile" branch → server).
Also tests Alpine (ID=alpine, not in id_to_clan) → clan=unknown, pkg_manager="".
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "alpine-lxc-container",
    "description": "Alpine Linux 3.19 musl, LXC container, near-empty /sys degenerate case",
    "os_release": (
        'NAME="Alpine Linux"\n'
        "ID=alpine\n"
        "VERSION_ID=3.19.1\n"
        'PRETTY_NAME="Alpine Linux v3.19"\n'
        'HOME_URL="https://alpinelinux.org/"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "cpu family\t: 25\n"
        "model\t\t: 1\n"
        "model name\t: AMD EPYC 7763 64-Core Processor\n"
        "stepping\t: 1\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {},  # LXC containers have no DMI
    "hwmon": [],  # no hwmon in minimal LXC
    "drm_cards": [],  # no DRM passthrough
    "modules": [],  # LXC shares host kernel modules but /sys/module may be empty inside
    "thermal_zones": 0,
    "powercap_rapl": False,
    "rapl_readable": False,
    "epp": {"present": False},
    "platform_profile": None,
    "perf_event_paranoid": 3,  # Alpine/LXC often restricts perf
    "secure_boot": None,  # no EFI vars in LXC
    "efi_vars": False,
    "power_supply": [],
    "env": {
        "TERM": "xterm-256color",
        "USER": "root",
        # no DBUS, no compositor
    },
    "which": {
        "asusctl": False,
        "ryzenadj": False,
        "hyprctl": False,
        "nvidia-smi": False,
        "ipmitool": False,
        "systemctl": False,  # Alpine uses OpenRC, not systemd
        "perf": False,
        "bpftrace": False,
        "apt": False,
        "dnf": False,
        "wmctrl": False,
        "xdotool": False,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": False},
    "subprocess": {},
    "expected": {
        "cpu_vendor": "amd",  # vendor_id line is present and says AuthenticAMD
        "cpu_arch": "x86_64",
        "distro_id": "alpine",
        "distro_clan": "unknown",  # alpine not in id_to_clan, no ID_LIKE
        "pkg_manager": "",  # unknown clan → empty pkg_manager
        "gpu_primary": "none",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "none",
        "facet": "server",  # compositor=none + no platform_profile → server
        "hwmon_k10temp": False,
        "hwmon_coretemp": False,
        "thermal_zone_count": 0,
        "rapl_readable": False,
        "epp_available": False,
        "epp_writable": False,
        "kmod_ipmi": False,
        "ipmitool_available": False,
        "is_virtual": False,  # no DMI → no dmi_hints match → False
        "secure_boot": None,
        "systemd_user_available": False,
        "expected_actuators": [],
    },
}
