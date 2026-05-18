"""Supermicro H12 series 1U — dual EPYC 7763 (128c/256t), AlmaLinux 9.

IPMI loaded (ipmi_si + ipmi_devintf), k10temp present, rapl_readable=False
(CVE-2020-8694 gate — root required on RHEL-family). No compositor, no
battery, secure_boot=True. ipmitool installed but no COOLSTEP_REDFISH_URL.

Asserts: facet=server (kmod_ipmi=True — strong server signal), cpu_vendor=amd,
distro_clan=fedora (alma maps to fedora), rapl_readable=False triggers
RAPL CVE warning, kmod_ipmi=True, ipmitool_available=True,
redfish_endpoint_configured=False.
Edge case: EPYC with k10temp but server facet — EPP on EPYC is available
but writable is platform-locked by BIOS.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "supermicro-epyc-h12-server",
    "description": "Supermicro H12 1U — dual EPYC 7763, AlmaLinux 9, IPMI, RAPL locked",
    "os_release": (
        'NAME="AlmaLinux"\n'
        "ID=alma\n"
        'ID_LIKE="rhel centos fedora"\n'
        "VERSION_ID=\"9.3\"\n"
        'PRETTY_NAME="AlmaLinux OS 9.3 (Shamrock Pampas Cat)"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: AuthenticAMD\n"
        "cpu family\t: 25\n"
        "model\t\t: 1\n"
        "model name\t: AMD EPYC 7763 64-Core Processor\n"
        "stepping\t: 1\n"
        "microcode\t: 0xa0011d1\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {
        "sys_vendor": "Supermicro",
        "product_name": "H12DSU-iN",
        "chassis_vendor": "Supermicro",
    },
    "hwmon": [
        {"name": "k10temp"},
        {"name": "k10temp"},  # second socket
        {"name": "ipmi"},
        {"name": "nvme"},
    ],
    "drm_cards": [],
    "modules": ["ipmi_si", "ipmi_devintf", "ipmi_msghandler", "k10temp"],
    "thermal_zones": 0,
    "powercap_rapl": True,
    "rapl_readable": False,  # CVE-2020-8694: root-only on AlmaLinux 9
    "epp": {"present": True, "value": "performance", "writable": False},
    "platform_profile": None,
    "perf_event_paranoid": 1,
    "secure_boot": True,
    "efi_vars": True,
    "power_supply": [],
    "env": {
        "TERM": "xterm-256color",
        # no COOLSTEP_REDFISH_URL, no compositor env
    },
    "which": {
        "asusctl": False,
        "ryzenadj": False,
        "hyprctl": False,
        "nvidia-smi": False,
        "ipmitool": True,
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
        "cpu_vendor": "amd",
        "cpu_arch": "x86_64",
        "distro_id": "alma",
        "distro_clan": "fedora",
        "pkg_manager": "dnf",
        "gpu_primary": "none",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "none",
        "facet": "server",
        "hwmon_k10temp": True,
        "kmod_ipmi": True,
        "ipmitool_available": True,
        "redfish_endpoint_configured": False,
        "rapl_readable": False,
        "epp_writable": False,
        "is_virtual": False,
        "secure_boot": True,
        "expected_actuators": [],
    },
}
