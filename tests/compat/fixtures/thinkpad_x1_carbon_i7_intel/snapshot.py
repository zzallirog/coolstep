"""ThinkPad X1 Carbon Gen 11 — Intel i7-1365U + Iris Xe.

Ubuntu 22.04 LTS, GNOME on Wayland, no dGPU, RAPL works as user, thinkpad_acpi
loaded so platform_profile present. Classic Intel ultrabook fingerprint.

Asserts: cpu_vendor=intel, gpu_primary=intel, facet=desktop (battery +
platform_profile), thinkpad_acpi fan available (thinkfan recommended),
EPP writable via intel_pstate driver, distro_clan=debian.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "thinkpad-x1-carbon-gen11-i7-1365u",
    "description": "ThinkPad X1 Carbon Gen 11 — i7-1365U + Iris Xe, Ubuntu 22.04 + GNOME",
    "os_release": (
        'NAME="Ubuntu"\n'
        "ID=ubuntu\n"
        "ID_LIKE=debian\n"
        "VERSION_ID=\"22.04\"\n"
        'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: GenuineIntel\n"
        "cpu family\t: 6\n"
        "model\t\t: 186\n"
        "model name\t: 13th Gen Intel(R) Core(TM) i7-1365U\n"
        "stepping\t: 3\n"
    ),
    "dmi": {
        "sys_vendor": "LENOVO",
        "product_name": "21HM005MUS",
        "chassis_vendor": "LENOVO",
    },
    "hwmon": [
        {"name": "coretemp"},
        {"name": "thinkpad"},
        {"name": "nvme"},
        {"name": "iwlwifi_1"},
        {"name": "BAT0"},
    ],
    "drm_cards": [
        {"vendor": "0x8086"},  # Iris Xe iGPU
    ],
    "modules": ["i915", "thinkpad_acpi"],
    "thermal_zones": 10,
    "powercap_rapl": True,
    "rapl_readable": True,
    "epp": {"present": True, "value": "balance_performance", "writable": True},
    "platform_profile": "balanced",
    "perf_event_paranoid": 4,  # Ubuntu locked it down
    "secure_boot": True,
    "efi_vars": True,
    "power_supply": ["BAT0", "AC"],
    "env": {
        "XDG_CURRENT_DESKTOP": "GNOME",
        "XDG_SESSION_TYPE": "wayland",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "WAYLAND_DISPLAY": "wayland-0",
        "GNOME_DESKTOP_SESSION_ID": "this-is-deprecated",
    },
    "which": {
        "asusctl": False, "ryzenadj": False, "hyprctl": False,
        "nvidia-smi": False, "ipmitool": False, "systemctl": True,
        "perf": True, "bpftrace": False, "apt": True, "thinkfan": False,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": False},
    "subprocess": {
        ("systemctl", "is-active", "tlp.service"): 3,
        ("systemctl", "is-active", "power-profiles-daemon.service"): 0,
    },
    "expected": {
        "cpu_vendor": "intel",
        "distro_id": "ubuntu",
        "distro_clan": "debian",
        "pkg_manager": "apt",
        "gpu_primary": "intel",
        "gpu_intel": True,
        "gpu_amd": False,
        "gpu_nvidia": False,
        "compositor": "mutter",
        "facet": "desktop",
        "hwmon_coretemp": True,
        "hwmon_k10temp": False,
        "asusctl_available": False,
        "epp_writable": True,
        "perf_event_paranoid": 4,
        "expected_community_pointer_ids": ["perf_paranoid"],
    },
}
