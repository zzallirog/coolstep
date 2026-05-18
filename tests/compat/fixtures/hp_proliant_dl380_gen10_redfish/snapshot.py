"""HP ProLiant DL380 Gen10 — dual Xeon Gold 5218, RHEL 9, iLO5 Redfish.

COOLSTEP_REDFISH_URL is set via env (iLO5 endpoint). coretemp, nvme and
ipmi_si modules loaded. ipmitool installed (so both `ipmi` and `redfish`
collectors will activate). No compositor, no battery, no platform_profile.

Asserts: facet=server (redfish_endpoint_configured=True — strong server
signal), cpu_vendor=intel, hwmon_coretemp=True, kmod_ipmi=True,
ipmitool_available=True, redfish_endpoint_configured=True,
distro_clan=fedora.
Edge case: Both ipmitool and Redfish present simultaneously — test that
the server facet fires from the redfish_endpoint_configured path even if
kmod_ipmi would also trigger it.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "hp-proliant-dl380-gen10-redfish",
    "description": "HP ProLiant DL380 Gen10 — 2x Xeon Gold 5218, RHEL 9, iLO5 Redfish",
    "os_release": (
        'NAME="Red Hat Enterprise Linux"\n'
        "ID=rhel\n"
        'ID_LIKE="fedora"\n'
        "VERSION_ID=\"9.2\"\n"
        'PRETTY_NAME="Red Hat Enterprise Linux 9.2 (Plow)"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: GenuineIntel\n"
        "cpu family\t: 6\n"
        "model\t\t: 85\n"
        "model name\t: Intel(R) Xeon(R) Gold 5218 CPU @ 2.30GHz\n"
        "stepping\t: 7\n"
    ),
    "uname_machine": "x86_64",
    "dmi": {
        "sys_vendor": "HP",
        "product_name": "ProLiant DL380 Gen10",
        "chassis_vendor": "HP",
    },
    "hwmon": [
        {"name": "coretemp"},
        {"name": "coretemp"},  # second socket
        {"name": "nvme"},
        {"name": "ipmi"},
    ],
    "drm_cards": [],
    "modules": ["ipmi_si", "ipmi_devintf", "coretemp", "nvme"],
    "thermal_zones": 0,
    "powercap_rapl": True,
    "rapl_readable": False,  # root-only on RHEL 9
    "epp": {"present": True, "value": "performance", "writable": False},
    "platform_profile": None,
    "perf_event_paranoid": 2,
    "secure_boot": True,
    "efi_vars": True,
    "power_supply": [],
    "env": {
        "COOLSTEP_REDFISH_URL": "https://10.0.1.50/redfish/v1/",
        "TERM": "xterm-256color",
        # no compositor
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
        "cpu_vendor": "intel",
        "cpu_arch": "x86_64",
        "distro_id": "rhel",
        "distro_clan": "fedora",
        "pkg_manager": "dnf",
        "gpu_primary": "none",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "none",
        "facet": "server",
        "hwmon_coretemp": True,
        "hwmon_k10temp": False,
        "kmod_ipmi": True,
        "ipmitool_available": True,
        "redfish_endpoint_configured": True,
        "rapl_readable": False,
        "epp_writable": False,
        "is_virtual": False,
        "secure_boot": True,
        "expected_actuators": [],
    },
}
