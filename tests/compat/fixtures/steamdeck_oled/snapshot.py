"""Steam Deck OLED — custom AMD Aerith APU, SteamOS 3.5 (Arch-based).

ID=steamos with ID_LIKE="arch", so distro_clan resolves to "arch" via
ID_LIKE fallback. gamescope compositor: env has WAYLAND_DISPLAY but no
known compositor env var → compositor="unknown". gamemoded.service is
active (Feral gamemode, NOT game-mode.service). ryzenadj present.
EPP=balance_performance. platform_profile=balanced.

Asserts: distro_clan=arch (via ID_LIKE), pkg_manager=pacman,
compositor=unknown (gamescope not in supported list, only XDG_SESSION_TYPE
wayland with no known WM env), facet=desktop (platform_profile present),
gpu_primary=amdgpu (AMD APU, no NVIDIA), ryzenadj_cap actuator activates.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "steamdeck-oled",
    "description": "Steam Deck OLED (AMD Aerith APU), SteamOS 3.5 Arch-based + gamescope",
    "os_release": (
        'NAME="SteamOS"\n'
        "ID=steamos\n"
        'ID_LIKE="arch"\n'
        "VERSION_ID=\"3.5\"\n"
        'PRETTY_NAME="SteamOS 3.5"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "cpu family\t: 23\n"
        "model\t\t: 144\n"
        "model name\t: AMD Custom APU 0405\n"
        "stepping\t: 1\n"
        "microcode\t: 0x8a00008\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {
        "sys_vendor": "Valve",
        "product_name": "Jupiter",
        "chassis_vendor": "Valve",
    },
    "hwmon": [
        {"name": "k10temp"},
        {"name": "amdgpu"},
        {"name": "nvme"},
    ],
    "drm_cards": [
        {"vendor": "0x1002"},  # AMD Aerith iGPU
    ],
    "modules": ["amdgpu"],
    "thermal_zones": 4,
    "powercap_rapl": True,
    "rapl_readable": True,
    "epp": {"present": True, "value": "balance_performance", "writable": True},
    "platform_profile": "balanced",
    "perf_event_paranoid": 2,
    "secure_boot": False,
    "efi_vars": True,
    "power_supply": ["BAT1", "ADP1"],
    "env": {
        # gamescope sets WAYLAND_DISPLAY but not HYPRLAND_INSTANCE_SIGNATURE or SWAYSOCK
        # XDG_CURRENT_DESKTOP is not KDE/GNOME/XFCE/OPENBOX
        "XDG_CURRENT_DESKTOP": "gamescope",
        "XDG_SESSION_TYPE": "wayland",
        "WAYLAND_DISPLAY": "wayland-0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    },
    "which": {
        "asusctl": False,
        "hyprctl": False,
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
        "pacman": True,
        "wmctrl": False,
        "xdotool": False,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": False},
    "subprocess": {
        ("systemctl", "is-active", "gamemoded.service"): 0,
        ("systemctl", "is-active", "tlp.service"): 3,
        ("systemctl", "is-active", "power-profiles-daemon.service"): 3,
    },
    "expected": {
        "cpu_vendor": "amd",
        "cpu_arch": "x86_64",
        "distro_id": "steamos",
        "distro_clan": "arch",
        "pkg_manager": "pacman",
        "gpu_primary": "amdgpu",
        "gpu_amd": True,
        "gpu_nvidia": False,
        "gpu_intel": False,
        # gamescope is not in supported compositor list; XDG_SESSION_TYPE=wayland → unknown
        "compositor": "unknown",
        # platform_profile present → desktop even with unknown compositor
        "facet": "desktop",
        "hwmon_k10temp": True,
        "asusctl_available": False,
        "ryzenadj_available": True,
        "epp_writable": True,
        "is_virtual": False,
        "expected_actuators": ["epp_shift", "ryzenadj_cap"],
    },
}
