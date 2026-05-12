"""ASUS TUF Gaming A15 FA507XV — the real target machine.

Ryzen 9 7940HS (Phoenix APU, Zen 4, Radeon 780M iGPU) + RTX 4060M dGPU.
Arch Linux + Hyprland + asusctl 6.1.2, ryzenadj installed, EPP available.

This is the production fingerprint coolstep was first calibrated against.
Asserts: dual-GPU detection, nvidia wins as primary, full P2 actuator set
available (asusctl + ryzenadj + EPP), facet=desktop.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "asus-tuf-a15-7940hs",
    "description": "ASUS TUF Gaming A15 FA507XV — Ryzen 9 7940HS + Radeon 780M + RTX 4060M, Arch + Hyprland",
    "os_release": (
        'NAME="Arch Linux"\n'
        'PRETTY_NAME="Arch Linux"\n'
        "ID=arch\n"
        'BUILD_ID=rolling\n'
        'ANSI_COLOR="38;2;23;147;209"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "cpu family\t: 25\n"
        "model\t\t: 116\n"
        "model name\t: AMD Ryzen 9 7940HS w/ Radeon 780M Graphics\n"
        "stepping\t: 1\n"
        "microcode\t: 0xa704107\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {
        "sys_vendor": "ASUSTeK COMPUTER INC.",
        "product_name": "ASUS TUF Gaming A15 FA507XV_FA507XV",
        "chassis_vendor": "ASUSTeK COMPUTER INC.",
    },
    "hwmon": [
        {"name": "k10temp"},
        {"name": "amdgpu"},
        {"name": "nvme"},
        {"name": "asus-nb-wmi"},
        {"name": "asus_wmi_sensors"},
    ],
    "drm_cards": [
        {"vendor": "0x1002"},  # AMD iGPU (Radeon 780M)
        {"vendor": "0x10de"},  # NVIDIA dGPU (RTX 4060M)
    ],
    "modules": ["amdgpu", "nvidia", "asus_wmi", "asus_nb_wmi"],
    "thermal_zones": 8,
    "powercap_rapl": True,
    "rapl_readable": True,
    "epp": {"present": True, "value": "balance_performance", "writable": True},
    "platform_profile": "balanced",
    "perf_event_paranoid": 2,
    "secure_boot": False,
    "efi_vars": True,
    "power_supply": ["BAT0", "ADP1"],
    "env": {
        "HYPRLAND_INSTANCE_SIGNATURE": "1747600000_test_session",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "XDG_SESSION_TYPE": "wayland",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "WAYLAND_DISPLAY": "wayland-0",
    },
    "hypr_socket": "1747600000_test_session",
    "which": {
        "asusctl": True, "hyprctl": True, "ryzenadj": True,
        "nvidia-smi": True, "systemctl": True, "ipmitool": False,
        "swaymsg": False, "thinkfan": False, "fancontrol": False,
        "nbfc": False, "coolercontrol": False, "perf": True,
        "bpftrace": False, "tlp": False, "auto-cpufreq": False,
        "pacman": True, "wmctrl": False, "xdotool": False,
    },
    "asusctl_version": "6.1.2",
    "py_modules": {"pynvml": True, "bcc": False},
    "subprocess": {
        ("systemctl", "is-active", "tlp.service"): 3,
        ("systemctl", "is-active", "power-profiles-daemon.service"): 0,
    },
    "expected": {
        "cpu_vendor": "amd",
        "cpu_arch": "x86_64",
        "distro_id": "arch",
        "distro_clan": "arch",
        "pkg_manager": "pacman",
        "gpu_primary": "nvidia",
        "gpu_amd": True,
        "gpu_nvidia": True,
        "gpu_intel": False,
        "compositor": "hyprland",
        "facet": "desktop",
        "hwmon_k10temp": True,
        "asusctl_available": True,
        "asusctl_version_ok": True,
        "ryzenadj_available": True,
        "epp_writable": True,
        "rapl_readable": True,
        "is_virtual": False,
        "secure_boot": False,
        "expected_actuators": ["asusctl_fan_curve_bias", "epp_shift", "ryzenadj_cap"],
        "expected_aur_packages": [],  # asusctl/ryzenadj already installed
    },
}
