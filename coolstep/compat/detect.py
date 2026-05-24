"""Platform detection — transcribes system state into PlatformCaps.

Pure I/O: reads /proc, /sys, /etc/os-release, shutil.which.
No writes, no subprocess side-effects beyond version probes.
No imports from coolstep.core (can run standalone).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from coolstep.compat.caps import (
    Compositor,
    CpuVendor,
    DistroClan,
    GpuStack,
    PlatformCaps,
)

log = logging.getLogger(__name__)

# /sys paths used during detection
_HWMON_ROOT = Path("/sys/class/hwmon")
_CPU_ROOT = Path("/sys/devices/system/cpu")
_DRM_ROOT = Path("/sys/class/drm")
_MODULE_ROOT = Path("/sys/module")
_RAPL_ROOT = Path("/sys/devices/virtual/powercap/intel-rapl")
_PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
_EFI_VARS = Path("/sys/firmware/efi/efivars")

# Distro / pkg-manager mappings live in coolstep/compat/core.json and are
# fetched via load_manifest() on demand.  L1/L2 layers can extend or override.


def _read(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "�" in raw:
        log.debug("detect: non-utf8 bytes replaced in %s", path)
    return raw.strip()


def _detect_distro() -> tuple[str, str, DistroClan, tuple[str, ...], str]:
    """Returns (id, name, clan, like_tuple, pkg_manager).

    Mapping tables (id→clan, clan→pkg_manager) come from the layered manifest.
    """
    from coolstep.compat.manifest import load_manifest
    manifest = load_manifest()
    id_to_clan: dict[str, str] = manifest.get("distro", {}).get("id_to_clan", {})
    clan_pkg_manager: dict[str, str] = manifest.get("distro", {}).get("clan_pkg_manager", {})

    os_release: dict[str, str] = {}
    for line in (_read(Path("/etc/os-release")) or "").splitlines():
        k, _, v = line.partition("=")
        os_release[k.strip()] = v.strip().strip('"')

    distro_id = os_release.get("ID", "").lower()
    distro_name = os_release.get("NAME", distro_id or "unknown")
    like_raw = os_release.get("ID_LIKE", "")
    distro_like: tuple[str, ...] = tuple(like_raw.lower().split()) if like_raw else ()

    clan_str: str = id_to_clan.get(distro_id, "")
    if not clan_str:
        for token in distro_like:
            if token in clan_pkg_manager:
                clan_str = token
                break
    clan: DistroClan = clan_str if clan_str in clan_pkg_manager else "unknown"  # type: ignore[assignment]
    pkg_manager = clan_pkg_manager.get(clan, "")

    return distro_id, distro_name, clan, distro_like, pkg_manager


def _detect_cpu() -> tuple[CpuVendor, str, set[str]]:
    """Returns (vendor, model_name, hwmon_names_set)."""
    cpu_info = _read(Path("/proc/cpuinfo")) or ""
    vendor: CpuVendor = "unknown"
    model = ""
    for line in cpu_info.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "vendor_id":
            if "AuthenticAMD" in v:
                vendor = "amd"
            elif "GenuineIntel" in v:
                vendor = "intel"
            elif "ARM" in v.upper() or "Qualcomm" in v:
                vendor = "arm"
        elif k == "model name" and not model:
            model = v

    hwmon_names: set[str] = set()
    if _HWMON_ROOT.is_dir():
        for hwmon in _HWMON_ROOT.iterdir():
            name = _read(hwmon / "name")
            if name:
                hwmon_names.add(name)

    return vendor, model, hwmon_names


def _detect_gpu() -> tuple[GpuStack, bool, bool, bool]:
    """Returns (primary_stack, amd, nvidia, intel).

    Vendor IDs come from manifest.gpu_vendors so new vendors can be added
    via community.json or custom.json without code changes.
    """
    from coolstep.compat.manifest import load_manifest
    vendors = load_manifest().get("gpu_vendors", {})
    amd_id = vendors.get("amd", "0x1002")
    nvidia_id = vendors.get("nvidia", "0x10de")
    intel_id = vendors.get("intel", "0x8086")

    amd = nvidia = intel = False

    if _DRM_ROOT.is_dir():
        for card in sorted(_DRM_ROOT.iterdir()):
            if not re.match(r"card\d+$", card.name):
                continue
            vendor_id = _read(card / "device" / "vendor")
            if vendor_id == amd_id:
                amd = True
            elif vendor_id == nvidia_id:
                nvidia = True
            elif vendor_id == intel_id:
                intel = True

    # Kernel modules as fallback
    if (_MODULE_ROOT / "amdgpu").is_dir():
        amd = True
    if (_MODULE_ROOT / "nvidia").is_dir():
        nvidia = True
    if (_MODULE_ROOT / "i915").is_dir() or (_MODULE_ROOT / "xe").is_dir():
        intel = True

    # Primary: dGPU preference (nvidia > amd > intel)
    primary: GpuStack
    if nvidia:
        primary = "nvidia"
    elif amd:
        primary = "amdgpu"
    elif intel:
        primary = "intel"
    else:
        primary = "none"

    return primary, amd, nvidia, intel


def _detect_compositor() -> Compositor:
    """Infer compositor from environment variables (best-effort)."""
    env = os.environ

    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if env.get("SWAYSOCK"):
        return "sway"

    xdg_de = env.get("XDG_CURRENT_DESKTOP", "").upper()
    xdg_session = env.get("XDG_SESSION_TYPE", "").lower()

    if "KDE" in xdg_de or env.get("KDE_FULL_SESSION"):
        return "kwin"
    if "GNOME" in xdg_de or env.get("GNOME_DESKTOP_SESSION_ID"):
        return "mutter"
    if "XFCE" in xdg_de:
        return "xfwm4"
    if "OPENBOX" in xdg_de:
        return "openbox"

    # Fallback: detect from binary presence (useful when running under systemd
    # without full env propagation)
    if shutil.which("hyprctl") and _hyprland_socket_exists():
        return "hyprland"
    if shutil.which("swaymsg") and env.get("WAYLAND_DISPLAY"):
        return "sway"

    if xdg_session == "wayland":
        return "unknown"
    if xdg_session == "x11" or env.get("DISPLAY"):
        return "x11_generic"

    return "none"


def _hyprland_socket_exists() -> bool:
    """Check for an active Hyprland socket in /run/user/<uid>/hypr/."""
    uid = os.getuid()
    hypr_dir = Path(f"/run/user/{uid}/hypr")
    if not hypr_dir.is_dir():
        return False
    return any((sig_dir / ".socket.sock").exists() for sig_dir in hypr_dir.iterdir())


def _asusctl_version() -> tuple[str, bool]:
    """Returns (version_string, major_ok). Empty string if not available.

    asusctl v6+ does not expose a --version flag.  We try several methods:
    1. `asusctl --version` (works on some older versions)
    2. `pacman -Q asusctl` (Arch-based)
    3. `dpkg -l asusctl` (Debian-based)
    4. `rpm -q asusctl` (Fedora/RHEL)
    """
    if not shutil.which("asusctl"):
        return "", False

    _ver_cmds: list[list[str]] = [
        ["asusctl", "--version"],
        ["pacman", "-Q", "asusctl"],
        ["dpkg", "-l", "asusctl"],
        ["rpm", "-q", "asusctl"],
    ]
    for cmd in _ver_cmds:
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
            combined = out.stdout + out.stderr
            m = re.search(r"(\d+)\.(\d+)[\.\d]*", combined)
            if m:
                ver = m.group(0)
                major_ok = int(m.group(1)) >= 6
                return ver, major_ok
        except Exception:  # noqa: BLE001
            continue
    # Binary exists but version undetectable → assume ok (presence implies pkg installed)
    return "unknown", True


def _service_active(name: str) -> bool:
    """Check if a systemd service is active (non-root safe)."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            timeout=2.0,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _epp_state(cpu_root: Path = _CPU_ROOT) -> tuple[bool, bool]:
    """Returns (readable, writable) for energy_performance_preference."""
    epp = cpu_root / "cpu0" / "cpufreq" / "energy_performance_preference"
    if not epp.exists():
        return False, False
    try:
        epp.read_text()
        readable = True
    except OSError:
        return False, False
    try:
        # Write back the current value — equivalent to no-op
        current = epp.read_text().strip()
        epp.write_text(current)
        writable = True
    except OSError:
        writable = False
    return readable, writable


def _secure_boot() -> bool | None:
    """Try to read SecureBoot EFI variable. None = can't tell."""
    if not _EFI_VARS.is_dir():
        return None
    try:
        for entry in _EFI_VARS.glob("SecureBoot-*"):
            data = entry.read_bytes()
            # EFI var layout: 4-byte attrs + 1-byte value
            if len(data) >= 5:
                return data[4] == 1
    except OSError:
        return None
    return None


def _pynvml_importable() -> bool:
    return importlib.util.find_spec("pynvml") is not None


def _bcc_importable() -> bool:
    return importlib.util.find_spec("bcc") is not None


def _cpu_arch() -> str:
    """Architecture string from `uname -m` equivalent."""
    try:
        return os.uname().machine
    except OSError:
        return "unknown"


def _is_virtual() -> bool:
    """Detect KVM/VMware/Xen/Hyper-V/cloud guest via DMI.

    Hint list comes from manifest.virtualization.dmi_hints — comunity can
    add new cloud vendors without modifying code.
    """
    from coolstep.compat.manifest import load_manifest
    hints = load_manifest().get("virtualization", {}).get("dmi_hints", [])
    sys_vendor = _read(Path("/sys/class/dmi/id/sys_vendor")) or ""
    product = _read(Path("/sys/class/dmi/id/product_name")) or ""
    chassis = _read(Path("/sys/class/dmi/id/chassis_vendor")) or ""
    needle = (sys_vendor + " " + product + " " + chassis).lower()
    return any(v in needle for v in hints)


def _rapl_readable(root: Path) -> bool:
    """Quick probe — try reading any energy_uj under powercap root."""
    if not root.is_dir():
        return False
    for pkg in root.glob("intel-rapl:*"):
        try:
            (pkg / "energy_uj").read_text()
            return True
        except OSError:
            continue
    return False


def _detect_superio(hwmon_names: set[str]) -> tuple[str, ...]:
    """Detect Super-I/O fan/temp chips from hwmon device names.

    Prefix list comes from manifest.hwmon.superio_prefixes.
    """
    from coolstep.compat.manifest import load_manifest
    prefixes = tuple(load_manifest().get("hwmon", {}).get("superio_prefixes", []))
    if not prefixes:
        return ()
    found = sorted({n for n in hwmon_names if n.startswith(prefixes)})
    return tuple(found)


def _thermal_zone_count() -> int:
    root = Path("/sys/class/thermal")
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir() if p.name.startswith("thermal_zone"))


def _perf_paranoid() -> int:
    raw = _read(Path("/proc/sys/kernel/perf_event_paranoid"))
    if raw is None:
        return 99
    try:
        return int(raw)
    except ValueError:
        return 99


def detect_caps(
    *,
    cpu_root: Path = _CPU_ROOT,
) -> PlatformCaps:
    """Run full platform detection and return an immutable PlatformCaps.

    All failures are silent — bad reads produce defaults (False / "unknown").
    Non-fatal issues are collected in caps.warnings.
    """
    warnings: list[str] = []

    distro_id, distro_name, distro_clan, distro_like, pkg_manager = _detect_distro()
    cpu_vendor, cpu_model, hwmon_names = _detect_cpu()
    gpu_primary, gpu_amd, gpu_nvidia, gpu_intel = _detect_gpu()
    compositor = _detect_compositor()
    asusctl_ver, asusctl_ver_ok = _asusctl_version()
    epp_readable, epp_writable = _epp_state(cpu_root)
    pynvml_ok = _pynvml_importable()

    # Warn if pynvml missing but NVIDIA GPU detected
    if gpu_nvidia and not pynvml_ok:
        warnings.append("NVIDIA GPU detected but pynvml not importable — pip install nvidia-ml-py")

    # Warn if no fan control found on a laptop (heuristic: battery present)
    has_battery = Path("/sys/class/power_supply").is_dir() and any(
        True for p in Path("/sys/class/power_supply").iterdir()
        if "BAT" in p.name.upper()
    )
    asusctl_ok = bool(shutil.which("asusctl"))
    nbfc_ok = bool(shutil.which("nbfc"))
    fancontrol_ok = bool(shutil.which("fancontrol"))
    thinkfan_ok = bool(shutil.which("thinkfan"))
    coolercontrol_ok = bool(shutil.which("coolercontrol"))
    if has_battery and not any([asusctl_ok, nbfc_ok, fancontrol_ok, thinkfan_ok, coolercontrol_ok]):
        warnings.append("Laptop detected but no fan control tool found (asusctl/nbfc/fancontrol/thinkfan)")

    # systemd user
    systemd_ok = bool(shutil.which("systemctl"))
    os.environ.get("XDG_RUNTIME_DIR", "")
    unit_dir = str(Path.home() / ".config" / "systemd" / "user") if systemd_ok else ""

    superio = _detect_superio(hwmon_names)
    rapl_powercap = _RAPL_ROOT.is_dir()
    rapl_readable = _rapl_readable(_RAPL_ROOT)
    if rapl_powercap and not rapl_readable:
        warnings.append(
            "RAPL energy counters present but unreadable (CVE-2020-8694 — "
            "root or CAP_SYS_ADMIN required for /sys/class/powercap/.../energy_uj)"
        )

    return PlatformCaps(
        # distro
        distro_id=distro_id,
        distro_name=distro_name,
        distro_clan=distro_clan,
        distro_like=distro_like,
        pkg_manager=pkg_manager,
        # cpu
        cpu_vendor=cpu_vendor,
        cpu_arch=_cpu_arch(),
        cpu_model=cpu_model,
        hwmon_k10temp="k10temp" in hwmon_names,
        hwmon_coretemp="coretemp" in hwmon_names,
        hwmon_zenpower="zenpower" in hwmon_names,
        hwmon_amd_energy="amd_energy" in hwmon_names,
        hwmon_rapl=rapl_powercap,
        rapl_powercap=rapl_powercap,
        rapl_readable=rapl_readable,
        hwmon_superio=superio,
        thermal_zone_count=_thermal_zone_count(),
        epp_available=epp_readable,
        epp_writable=epp_writable,
        platform_profile_available=_PLATFORM_PROFILE.exists(),
        perf_event_paranoid=_perf_paranoid(),
        # gpu
        gpu_primary=gpu_primary,
        gpu_amd=gpu_amd,
        gpu_nvidia=gpu_nvidia,
        gpu_intel=gpu_intel,
        pynvml_importable=pynvml_ok,
        # compositor
        compositor=compositor,
        hyprctl_available=bool(shutil.which("hyprctl")),
        swaymsg_available=bool(shutil.which("swaymsg")),
        wmctrl_available=bool(shutil.which("wmctrl")),
        xdotool_available=bool(shutil.which("xdotool")),
        # fan tools
        asusctl_available=asusctl_ok,
        asusctl_version=asusctl_ver,
        asusctl_version_ok=asusctl_ver_ok,
        nbfc_available=nbfc_ok,
        fancontrol_available=fancontrol_ok,
        thinkfan_available=thinkfan_ok,
        dell_smm_hwmon=(_MODULE_ROOT / "dell_smm_hwmon").is_dir(),
        coolercontrol_available=coolercontrol_ok,
        # power tools
        ryzenadj_available=bool(shutil.which("ryzenadj")),
        auto_cpufreq_available=bool(shutil.which("auto-cpufreq")),
        tlp_available=bool(shutil.which("tlp")),
        tlp_active=_service_active("tlp.service"),
        power_profiles_daemon_active=_service_active("power-profiles-daemon.service"),
        nvidia_smi_available=bool(shutil.which("nvidia-smi")),
        # systemd
        systemd_user_available=systemd_ok,
        systemd_unit_user_dir=unit_dir,
        # kernel modules
        kmod_amdgpu=(_MODULE_ROOT / "amdgpu").is_dir(),
        kmod_nvidia=(_MODULE_ROOT / "nvidia").is_dir(),
        kmod_i915=(_MODULE_ROOT / "i915").is_dir(),
        kmod_xe=(_MODULE_ROOT / "xe").is_dir(),
        kmod_ipmi=(_MODULE_ROOT / "ipmi_si").is_dir() or (_MODULE_ROOT / "ipmi_devintf").is_dir(),
        kmod_dell_smm_hwmon=(_MODULE_ROOT / "dell_smm_hwmon").is_dir(),
        # enterprise / server
        ipmitool_available=bool(shutil.which("ipmitool")),
        redfish_endpoint_configured=bool(os.environ.get("COOLSTEP_REDFISH_URL", "")),
        bpftrace_available=bool(shutil.which("bpftrace")),
        bcc_importable=_bcc_importable(),
        perf_binary_available=bool(shutil.which("perf")),
        dbus_session_active=bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")),
        # misc
        secure_boot=_secure_boot(),
        is_virtual=_is_virtual(),
        # metadata
        detected_at=time.time(),
        warnings=tuple(warnings),
    )
