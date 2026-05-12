"""Intel homelab desktop tower — i5-13500 + Intel UHD 770 (i915), ASUS board.

Debian 13 (trixie), headless (no graphical session, no compositor), pip-installed
coolstep, no fan-control tools, RAPL locked (CVE-2020-8694), no platform_profile,
two NVMe drives exposed via hwmon (nvme × 2), eeepc-wmi hwmon present (ASUS board
quirk even on desktop), EPP present but read-only.

Asserts: facet=server (headless physical host — no compositor, no platform_profile),
rapl_perm community pointer raised, no actuators except readonly_log,
cpu_vendor=intel, gpu_intel=True.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "intel-desktop-homelab-i5-13500",
    "description": "i5-13500 homelab tower, ASUS board, Debian 13 headless, pip coolstep",
    "os_release": (
        'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
        'NAME="Debian GNU/Linux"\n'
        "VERSION_ID=\"13\"\n"
        'VERSION="13 (trixie)"\n'
        "VERSION_CODENAME=trixie\n"
        "ID=debian\n"
        'HOME_URL="https://www.debian.org/"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: GenuineIntel\n"
        "cpu family\t: 6\n"
        "model\t\t: 191\n"
        "model name\t: 13th Gen Intel(R) Core(TM) i5-13500\n"
        "stepping\t: 2\n"
        "microcode\t: 0x3d\n"
        "cpu MHz\t\t: 800.000\n"
        "cache size\t: 24576 KB\n"
        "physical id\t: 0\n"
        "siblings\t: 20\n"
        "core id\t\t: 0\n"
        "cpu cores\t: 14\n"
    ),
    "dmi": {
        "sys_vendor": "ASUS",
        "product_name": "System Product Name",
    },
    "hwmon": [
        {"name": "acpitz"},   # hwmon0 — ACPI thermal zone
        {"name": "nvme"},     # hwmon1 — NVMe slot 1
        {"name": "nvme"},     # hwmon2 — NVMe slot 2
        {"name": "asus"},     # hwmon3 — eeepc-wmi (ASUS board quirk)
        {"name": "coretemp"}, # hwmon4 — Intel core temp
    ],
    "drm_cards": [
        {"vendor": "0x8086"},  # Intel UHD 770 (i915)
    ],
    "modules": ["i915", "coretemp", "intel_pstate", "thermal"],
    "thermal_zones": 2,
    "powercap_rapl": True,
    "rapl_readable": False,   # CVE-2020-8694: permission denied without CAP_SYS_ADMIN
    "epp": {"present": True, "value": "performance", "writable": False},
    "platform_profile": None, # desktop tower — no ACPI platform profile cycle
    "perf_event_paranoid": 3, # Debian 13 default: restrictive
    "secure_boot": False,     # SecureBoot EFI var byte = 0x00
    "efi_vars": True,
    "power_supply": [],       # tower — no battery
    "env": {
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "TERM": "xterm-256color",
        # no DBUS_SESSION_BUS_ADDRESS, no WAYLAND_DISPLAY, no compositor env
    },
    "which": {
        "asusctl": False, "ryzenadj": False, "hyprctl": False,
        "nvidia-smi": False, "ipmitool": False, "intel_gpu_top": False,
        "bpftrace": False, "lm_sensors": False, "fancontrol": False,
        "thinkfan": False, "tlp": False, "powerprofilesctl": False,
        "systemctl": True, "apt": True,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": False, "psutil": True, "fastapi": True},
    "subprocess": {
        ("systemctl", "is-active", "power-profiles-daemon"): 3,  # inactive
        ("systemctl", "is-active", "tlp"): 3,
        ("systemctl", "is-active", "thermald"): 3,
    },
    "expected": {
        "cpu_vendor": "intel",
        "distro_id": "debian",
        "distro_clan": "debian",
        "pkg_manager": "apt",
        "gpu_primary": "intel",
        "gpu_intel": True,
        "gpu_amd": False,
        "gpu_nvidia": False,
        "compositor": "none",
        "facet": "server",        # headless: no compositor + no platform_profile
        "rapl_readable": False,
        "kmod_ipmi": False,
        "ipmitool_available": False,
        "redfish_endpoint_configured": False,
        "expected_actuators": [],
        "expected_community_pointer_ids": ["rapl_perm"],
    },
}
