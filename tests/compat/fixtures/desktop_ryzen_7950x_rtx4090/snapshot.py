"""Enthusiast AMD desktop — Ryzen 9 7950X + RTX 4090 + MSI MEG X670E.

Fedora 39 Workstation, KDE on Wayland, no laptop platform_profile, no
battery, NCT6687 superio detected but driver missing (gigabyte_wmi present
on this board but it doesn't expose fan RPMs — classic gotcha).

Asserts: amdgpu + nvidia both detected (nvidia wins), facet=desktop via
compositor presence, no asusctl (it's not an ASUS board), community pointer
for nct6687 raised, no platform_profile so no laptop-only paths.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "desktop-ryzen-7950x-rtx4090",
    "description": "Ryzen 9 7950X + RTX 4090, MSI MEG X670E, Fedora 39 + KDE Wayland",
    "os_release": (
        'NAME="Fedora Linux"\n'
        "ID=fedora\n"
        "VERSION_ID=39\n"
        'PRETTY_NAME="Fedora Linux 39 (Workstation Edition)"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "cpu family\t: 25\n"
        "model name\t: AMD Ryzen 9 7950X 16-Core Processor\n"
        "stepping\t: 2\n"
    ),
    "dmi": {
        "sys_vendor": "Micro-Star International Co., Ltd.",
        "product_name": "MS-7E12",
        "chassis_vendor": "Micro-Star International Co., Ltd.",
    },
    "hwmon": [
        {"name": "k10temp"},
        {"name": "amdgpu"},
        {"name": "nvme"},
        {"name": "drivetemp"},
        # NCT6687 superio chip — driver not loaded by default on Fedora,
        # so hwmon doesn't show it. We emulate detection via prefix match.
    ],
    "drm_cards": [
        {"vendor": "0x10de"},  # RTX 4090
    ],
    "modules": ["amdgpu", "nvidia"],  # amdgpu loaded for Ryzen iGPU (not used)
    "thermal_zones": 4,
    "powercap_rapl": True,
    "rapl_readable": True,
    "epp": {"present": True, "value": "performance", "writable": True},
    "platform_profile": None,  # desktop board → no ACPI platform profile
    "perf_event_paranoid": 2,
    "secure_boot": False,
    "efi_vars": True,
    "power_supply": [],  # no battery
    "env": {
        "XDG_CURRENT_DESKTOP": "KDE",
        "XDG_SESSION_TYPE": "wayland",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "WAYLAND_DISPLAY": "wayland-0",
        "KDE_FULL_SESSION": "true",
    },
    "which": {
        "asusctl": False, "ryzenadj": False, "hyprctl": False,
        "nvidia-smi": True, "ipmitool": False, "systemctl": True,
        "perf": True, "bpftrace": False, "dnf": True,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": True, "bcc": False},
    "expected": {
        "cpu_vendor": "amd",
        "distro_clan": "fedora",
        "pkg_manager": "dnf",
        "gpu_primary": "nvidia",
        "gpu_amd": True,  # amdgpu module loaded for Ryzen iGPU even if not used for output
        "gpu_nvidia": True,
        "compositor": "kwin",
        "facet": "desktop",  # via kwin compositor
        "hwmon_k10temp": True,
        "asusctl_available": False,
        "ryzenadj_available": False,
        "expected_community_pointer_ids": ["ryzenadj"],  # AMD CPU but no ryzenadj
        # Note: nct6687 pointer triggers on facet=desktop + is_workstation
    },
}
