"""Mac Mini Late 2014 (i5-4278U Haswell) running Fedora 39.

applesmc hwmon driver present (Apple SMC fan + thermal). coretemp also
loaded. i915 module (Intel HD Graphics 5000). No battery (Mac Mini is a
desktop, no BAT0). perf_paranoid=2. No asusctl, no ryzenadj (Intel).

Asserts: cpu_vendor=intel, gpu_primary=intel (i915 module loaded), no
battery so has_battery=False, distro_clan=fedora, pkg_manager=dnf,
hwmon_coretemp=True. perf_event_paranoid=2.
Edge case: applesmc in hwmon_superio — the superio prefix detection must
pick up "applesmc" (if in manifest prefix list) or at minimum not crash.
Also tests Fedora on Apple hardware (rare but real).
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "mac-mini-intel-haswell-fedora39",
    "description": "Mac Mini Late 2014 (i5-4278U), Fedora 39, applesmc hwmon, i915",
    "os_release": (
        'NAME="Fedora Linux"\n'
        "ID=fedora\n"
        "VERSION_ID=39\n"
        'PRETTY_NAME="Fedora Linux 39 (Workstation Edition)"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: GenuineIntel\n"
        "cpu family\t: 6\n"
        "model\t\t: 69\n"
        "model name\t: Intel(R) Core(TM) i5-4278U CPU @ 2.60GHz\n"
        "stepping\t: 1\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {
        "sys_vendor": "Apple Inc.",
        "product_name": "Macmini7,1",
        "chassis_vendor": "Apple Inc.",
    },
    "hwmon": [
        {"name": "coretemp"},
        {"name": "applesmc"},
        {"name": "nvme"},
    ],
    "drm_cards": [
        {"vendor": "0x8086"},  # Intel HD Graphics 5000
    ],
    "modules": ["i915", "applesmc"],
    "thermal_zones": 3,
    "powercap_rapl": True,
    "rapl_readable": True,
    "epp": {"present": False},  # Haswell/broadwell era predates HWP/EPP
    "platform_profile": None,  # no ACPI platform_profile on Mac Mini under Linux
    "perf_event_paranoid": 2,
    "secure_boot": None,  # Apple EFI: no standard SecureBoot EFI var
    "efi_vars": False,
    "power_supply": [],  # desktop, no battery
    "env": {
        "XDG_CURRENT_DESKTOP": "GNOME",
        "XDG_SESSION_TYPE": "wayland",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "WAYLAND_DISPLAY": "wayland-0",
        "GNOME_DESKTOP_SESSION_ID": "this-is-deprecated",
    },
    "which": {
        "asusctl": False,
        "ryzenadj": False,
        "hyprctl": False,
        "nvidia-smi": False,
        "ipmitool": False,
        "systemctl": True,
        "perf": True,
        "bpftrace": False,
        "dnf": True,
        "rpm": True,
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
        "cpu_vendor": "intel",
        "cpu_arch": "x86_64",
        "distro_id": "fedora",
        "distro_clan": "fedora",
        "pkg_manager": "dnf",
        "gpu_primary": "intel",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": True,
        "compositor": "mutter",
        "facet": "desktop",  # GNOME compositor → desktop even without battery
        "hwmon_coretemp": True,
        "hwmon_k10temp": False,
        "asusctl_available": False,
        "ryzenadj_available": False,
        "epp_available": False,
        "epp_writable": False,
        "rapl_readable": True,
        "perf_event_paranoid": 2,
        "is_virtual": False,
        "secure_boot": None,  # no EFI vars → None
        "kmod_i915": True,
    },
}
