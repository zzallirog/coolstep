"""Raspberry Pi 5 — Broadcom BCM2712 (Cortex-A76), 8GB.

Raspberry Pi OS (Debian 12 bookworm), no compositor (headless), aarch64.
ACPI thermal zones present (cpu_thermal + soc_thermal), no RAPL, no EPP,
no fan control tool. Edge case: cpu_vendor heuristic must handle ARM.

Asserts: cpu_vendor=arm, arch=aarch64, gpu_primary=none, distro=debian,
facet=server (no compositor), arm_thermal collector activates via thermal_zones>0,
no RAPL/EPP recommendations.
"""
from __future__ import annotations

SNAPSHOT: dict = {
    "name": "raspberry-pi-5-arm",
    "description": "Raspberry Pi 5 (BCM2712 Cortex-A76), Raspberry Pi OS bookworm headless",
    "os_release": (
        'PRETTY_NAME="Raspbian GNU/Linux 12 (bookworm)"\n'
        "NAME=\"Raspbian GNU/Linux\"\n"
        "ID=raspbian\n"
        "ID_LIKE=debian\n"
        "VERSION_ID=\"12\"\n"
    ),
    "cpuinfo": (
        "processor\t: 0\n"
        "BogoMIPS\t: 108.00\n"
        "Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm jscvt fcma lrcpc dcpop sha3 sm3 sm4 asimddp sha512 asimdfhm dit uscat ilrcpc flagm ssbs sb paca pacg dcpodp flagm2 frint\n"
        "CPU implementer\t: 0x41\n"
        "CPU architecture: 8\n"
        "CPU variant\t: 0x4\n"
        "CPU part\t: 0xd0b\n"
        "Hardware\t: BCM2712\n"
        "Model\t\t: Raspberry Pi 5 Model B Rev 1.0\n"
        "model name\t: ARMv8 Processor\n"
    ),
    "uname_machine": "aarch64",
    "dmi": {},  # no DMI on RPi
    "hwmon": [
        {"name": "cpu_thermal"},
        {"name": "rp1_adc"},
    ],
    "drm_cards": [],  # VideoCore VII not a discrete vendor ID
    "modules": [],
    "thermal_zones": 2,
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
        "USER": "pi",
    },
    "which": {
        "asusctl": False, "ryzenadj": False, "hyprctl": False,
        "nvidia-smi": False, "ipmitool": False, "systemctl": True,
        "perf": False, "bpftrace": False, "apt": True, "dpkg": True,
    },
    "asusctl_version": "",
    "py_modules": {"pynvml": False, "bcc": False},
    "expected": {
        "cpu_vendor": "unknown",  # /proc/cpuinfo lacks vendor_id on ARM; lib falls back
        "cpu_arch": "aarch64",
        "distro_id": "raspbian",
        "distro_clan": "debian",
        "pkg_manager": "apt",
        "gpu_primary": "none",
        "gpu_amd": False,
        "gpu_nvidia": False,
        "gpu_intel": False,
        "compositor": "none",
        "facet": "server",  # headless ARM SBC → server-class packaging
        "thermal_zone_count": 2,
        "rapl_readable": False,
        "epp_writable": False,
    },
}
