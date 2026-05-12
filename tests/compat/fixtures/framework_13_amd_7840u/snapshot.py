"""Framework 13 (AMD Ryzen 7 7840U) — AMD APU only, no dGPU.

Debian 12 bookworm + Hyprland. No asusctl (not an ASUS laptop), ryzenadj
present, framework_laptop kmod loaded. EPP writable. secure_boot=False.
Has battery (BAT1 via Framework's EC).

Asserts: gpu_primary=amdgpu (AMD only, no nvidia), facet=desktop (Hyprland
compositor), ryzenadj actuator activates, epp_shift activates, no asusctl.
Edge case: APU-only laptop where nvidia check must NOT fire even though
amdgpu module is loaded.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "framework-13-amd-7840u",
    "description": "Framework 13 (Ryzen 7 7840U), Debian 12 + Hyprland, AMD APU only",
    "os_release": (
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
        'NAME="Debian GNU/Linux"\n'
        "ID=debian\n"
        "VERSION_ID=\"12\"\n"
        'VERSION="12 (bookworm)"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "cpu family\t: 25\n"
        "model\t\t: 116\n"
        "model name\t: AMD Ryzen 7 7840U w/ Radeon 780M Graphics\n"
        "stepping\t: 1\n"
        "microcode\t: 0xa704106\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {
        "sys_vendor": "Framework",
        "product_name": "Laptop 13 (AMD Ryzen 7040Series)",
        "chassis_vendor": "Framework",
    },
    "hwmon": [
        {"name": "k10temp"},
        {"name": "amdgpu"},
        {"name": "nvme"},
        {"name": "framework_laptop"},
    ],
    "drm_cards": [
        {"vendor": "0x1002"},  # Radeon 780M iGPU — only GPU
    ],
    "modules": ["amdgpu", "framework_laptop"],
    "thermal_zones": 6,
    "powercap_rapl": True,
    "rapl_readable": True,
    "epp": {"present": True, "value": "balance_performance", "writable": True},
    "platform_profile": "balanced",
    "perf_event_paranoid": 2,
    "secure_boot": False,
    "efi_vars": True,
    "power_supply": ["BAT1", "ADP1"],
    "env": {
        "HYPRLAND_INSTANCE_SIGNATURE": "1747700000_fw13_session",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "XDG_SESSION_TYPE": "wayland",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "WAYLAND_DISPLAY": "wayland-1",
    },
    "hypr_socket": "1747700000_fw13_session",
    "which": {
        "asusctl": False,
        "hyprctl": True,
        "ryzenadj": True,
        "nvidia-smi": False,
        "systemctl": True,
        "ipmitool": False,
        "swaymsg": False,
        "thinkfan": False,
        "fancontrol": False,
        "nbfc": False,
        "coolercontrol": False,
        "perf": True,
        "bpftrace": False,
        "tlp": False,
        "auto-cpufreq": False,
        "apt": True,
        "dpkg": True,
        "wmctrl": False,
        "xdotool": False,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": False},
    "subprocess": {
        ("systemctl", "is-active", "tlp.service"): 3,
        ("systemctl", "is-active", "power-profiles-daemon.service"): 3,
    },
    "expected": {
        "cpu_vendor": "amd",
        "cpu_arch": "x86_64",
        "distro_id": "debian",
        "distro_clan": "debian",
        "pkg_manager": "apt",
        "gpu_primary": "amdgpu",
        "gpu_amd": True,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "hyprland",
        "facet": "desktop",
        "hwmon_k10temp": True,
        "asusctl_available": False,
        "ryzenadj_available": True,
        "epp_writable": True,
        "rapl_readable": True,
        "is_virtual": False,
        "secure_boot": False,
        "expected_actuators": ["epp_shift", "ryzenadj_cap"],
    },
}
