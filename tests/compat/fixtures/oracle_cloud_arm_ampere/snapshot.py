"""Oracle Cloud Free Tier — Ampere A1 (Cortex-A78AE), Ubuntu 22.04 aarch64.

OCI VMs are KVM-based; sys_vendor contains "openstack" (OCI uses Nova/KVM
under the hood and reports as OpenStack for DMI). No EPP (aarch64 + cloud
guest). No thermal zones (cloud guest — no /sys/class/thermal exports).
No DRM cards, no physical GPU. cpu_vendor=arm (uname_machine=aarch64).

Asserts: cpu_arch=aarch64, cpu_vendor=arm (Cortex-A78AE has no vendor_id
line — falls back to "unknown" since ARM keyword not in our fake cpuinfo
vendor_id), is_virtual=True ("openstack" matches dmi_hints), facet=server
(is_virtual + no platform_profile), epp_available=False,
distro_clan=debian (ubuntu).
Edge case: ARM cloud guest where neither EPP nor thermal zones expose.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "oracle-cloud-arm-ampere-a1",
    "description": "Oracle Cloud Ampere A1 (Cortex-A78AE), Ubuntu 22.04 aarch64, KVM guest",
    "os_release": (
        'NAME="Ubuntu"\n'
        "ID=ubuntu\n"
        "ID_LIKE=debian\n"
        "VERSION_ID=\"22.04\"\n"
        'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'
    ),
    "cpuinfo": (
        # aarch64 CPUs don't expose vendor_id in the same way
        "processor\t: 0\n"
        "BogoMIPS\t: 50.00\n"
        "Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid\n"
        "CPU implementer\t: 0x41\n"
        "CPU architecture: 8\n"
        "CPU variant\t: 0x1\n"
        "CPU part\t: 0xd47\n"
        "model name\t: Neoverse-N1\n"
    ),
    "uname_machine": "aarch64",
    "dmi": {
        "sys_vendor": "OpenStack Foundation",
        "product_name": "OpenStack Nova",
        "chassis_vendor": "OpenStack Foundation",
    },
    "hwmon": [],
    "drm_cards": [],
    "modules": [],
    "thermal_zones": 0,
    "powercap_rapl": False,
    "rapl_readable": False,
    "epp": {"present": False},
    "platform_profile": None,
    "perf_event_paranoid": 2,
    "secure_boot": None,
    "efi_vars": False,
    "power_supply": [],
    "env": {
        "TERM": "xterm-256color",
        "USER": "ubuntu",
        # no compositor, no DBUS session
    },
    "which": {
        "asusctl": False,
        "ryzenadj": False,
        "hyprctl": False,
        "nvidia-smi": False,
        "ipmitool": False,
        "systemctl": True,
        "perf": False,
        "bpftrace": False,
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
        "cpu_vendor": "unknown",  # aarch64 /proc/cpuinfo has no vendor_id field
        "cpu_arch": "aarch64",
        "distro_id": "ubuntu",
        "distro_clan": "debian",
        "pkg_manager": "apt",
        "gpu_primary": "none",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "none",
        "facet": "server",  # is_virtual=True + no platform_profile → server
        "epp_available": False,
        "epp_writable": False,
        "rapl_readable": False,
        "thermal_zone_count": 0,
        "is_virtual": True,  # "openstack" in dmi_hints
        "kmod_ipmi": False,
        "ipmitool_available": False,
        "redfish_endpoint_configured": False,
        "expected_actuators": [],
    },
}
