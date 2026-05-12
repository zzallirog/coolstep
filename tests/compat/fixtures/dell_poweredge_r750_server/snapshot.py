"""Dell PowerEdge R750 — dual Xeon Gold 6338 + iDRAC9 + Redfish.

RHEL 9 minimal, no compositor, no battery, ipmi_si/ipmi_devintf loaded,
ipmitool installed, COOLSTEP_REDFISH_URL set. Classic rack-server fingerprint.

Asserts: facet=server (no compositor, no platform_profile, ipmi present),
both `ipmi` and `redfish` collectors will activate, actuators stay disabled
(BMC owns chassis fans), RAPL counters require CAP_SYS_ADMIN.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "dell-poweredge-r750-server",
    "description": "Dell PowerEdge R750 — 2x Xeon Gold 6338 + iDRAC9 Redfish, RHEL 9",
    "os_release": (
        'NAME="Red Hat Enterprise Linux"\n'
        "ID=rhel\n"
        'ID_LIKE="fedora"\n'
        "VERSION_ID=\"9.3\"\n"
        'PRETTY_NAME="Red Hat Enterprise Linux 9.3 (Plow)"\n'
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "vendor_id\t: GenuineIntel\n"
        "cpu family\t: 6\n"
        "model\t\t: 106\n"
        "model name\t: Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz\n"
        "stepping\t: 6\n"
    ),
    "dmi": {
        "sys_vendor": "Dell Inc.",
        "product_name": "PowerEdge R750",
        "chassis_vendor": "Dell Inc.",
    },
    "hwmon": [
        {"name": "coretemp"},
        {"name": "coretemp"},  # second socket
        {"name": "nvme"},
        {"name": "ipmi_si"},
    ],
    "drm_cards": [
        {"vendor": "0x1a03"},  # ASPEED AST2500 BMC graphics (not detected as AMD/NVIDIA/Intel)
    ],
    "modules": ["ipmi_si", "ipmi_devintf", "ipmi_msghandler"],
    "thermal_zones": 0,  # RHEL minimal often has no ACPI thermal zones
    "powercap_rapl": True,
    "rapl_readable": False,  # CVE-2020-8694: root-only on RHEL 9
    "epp": {"present": True, "value": "performance", "writable": False},  # locked by Dell BIOS
    "platform_profile": None,  # no laptop profile cycle
    "perf_event_paranoid": 1,
    "secure_boot": True,
    "efi_vars": True,
    "power_supply": [],
    "env": {
        "COOLSTEP_REDFISH_URL": "https://192.168.20.10/redfish/v1/",
        "TERM": "xterm-256color",
        # no DBUS, no XDG, no compositor
    },
    "which": {
        "asusctl": False, "ryzenadj": False, "hyprctl": False,
        "nvidia-smi": False, "ipmitool": True, "systemctl": True,
        "perf": True, "bpftrace": True, "dnf": True, "rpm": True,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": True},
    "expected": {
        "cpu_vendor": "intel",
        "distro_id": "rhel",
        "distro_clan": "fedora",
        "pkg_manager": "dnf",
        "gpu_primary": "none",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "none",
        "facet": "server",
        "kmod_ipmi": True,
        "ipmitool_available": True,
        "redfish_endpoint_configured": True,
        "epp_writable": False,
        "rapl_readable": False,
        # facet=server suppresses some pointers; rapl_perm should still surface
        "expected_community_pointer_ids": ["rapl_perm"],
    },
}
