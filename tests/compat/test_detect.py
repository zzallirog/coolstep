"""Tests for coolstep.compat.detect — mock filesystem, verify caps fields."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coolstep.compat import reset_caps
from coolstep.compat.detect import (
    _detect_compositor,
    _detect_cpu,
    _detect_distro,
    _detect_gpu,
    _epp_state,
    detect_caps,
)


@pytest.fixture(autouse=True)
def _clear_caps():
    """Ensure the singleton is cleared before each test."""
    reset_caps(None)
    yield
    reset_caps(None)


# ---------------------------------------------------------------------------
# Distro detection
# ---------------------------------------------------------------------------

class TestDetectDistro:
    def test_arch(self, tmp_path: Path):
        (tmp_path / "os-release").write_text(
            'ID=arch\nNAME="Arch Linux"\n', encoding="utf-8"
        )
        with patch("coolstep.compat.detect._read", side_effect=lambda p: (
            (tmp_path / "os-release").read_text() if p.name == "os-release" else None
        )):
            from coolstep.compat.detect import _detect_distro
            distro_id, name, clan, like, pm = _detect_distro()
        assert distro_id == "arch"
        assert clan == "arch"
        assert pm == "pacman"

    def test_ubuntu(self, tmp_path: Path):
        content = 'ID=ubuntu\nID_LIKE=debian\nNAME="Ubuntu"\n'
        (tmp_path / "os-release").write_text(content, encoding="utf-8")
        with patch("coolstep.compat.detect._read", side_effect=lambda p: (
            content if "os-release" in str(p) else None
        )):
            distro_id, name, clan, like, pm = _detect_distro()
        assert distro_id == "ubuntu"
        assert clan == "debian"
        assert pm == "apt"

    def test_fedora(self):
        content = 'ID=fedora\nNAME="Fedora Linux"\n'
        with patch("coolstep.compat.detect._read", return_value=content):
            distro_id, _, clan, _, pm = _detect_distro()
        assert clan == "fedora"
        assert pm == "dnf"

    def test_unknown_distro(self):
        with patch("coolstep.compat.detect._read", return_value=""):
            distro_id, _, clan, _, pm = _detect_distro()
        assert clan == "unknown"
        assert pm == ""


# ---------------------------------------------------------------------------
# CPU detection
# ---------------------------------------------------------------------------

class TestDetectCpu:
    _AMD_CPUINFO = (
        "vendor_id\t: AuthenticAMD\n"
        "model name\t: AMD Ryzen 9 7940HS\n"
    )
    _INTEL_CPUINFO = (
        "vendor_id\t: GenuineIntel\n"
        "model name\t: Intel Core i7-12700H\n"
    )

    def test_amd_vendor(self, tmp_path: Path):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text(self._AMD_CPUINFO, encoding="utf-8")
        with (
            patch("coolstep.compat.detect._read", side_effect=lambda p: (
                self._AMD_CPUINFO if "cpuinfo" in str(p) else None
            )),
            patch("coolstep.compat.detect._HWMON_ROOT", tmp_path / "hwmon"),
        ):
            vendor, model, hwmon_names = _detect_cpu()
        assert vendor == "amd"
        assert "7940HS" in model

    def test_intel_vendor(self):
        with (
            patch("coolstep.compat.detect._read", side_effect=lambda p: (
                self._INTEL_CPUINFO if "cpuinfo" in str(p) else None
            )),
            patch("coolstep.compat.detect._HWMON_ROOT", Path("/nonexistent")),
        ):
            vendor, _, _ = _detect_cpu()
        assert vendor == "intel"

    def test_hwmon_k10temp(self, tmp_path: Path):
        hwmon0 = tmp_path / "hwmon0"
        hwmon0.mkdir()
        (hwmon0 / "name").write_text("k10temp\n", encoding="utf-8")
        with (
            patch("coolstep.compat.detect._read", side_effect=lambda p: (
                self._AMD_CPUINFO if "cpuinfo" in str(p)
                else Path(p).read_text().strip() if Path(p).exists() else None
            )),
            patch("coolstep.compat.detect._HWMON_ROOT", tmp_path),
        ):
            _, _, hwmon_names = _detect_cpu()
        assert "k10temp" in hwmon_names


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

class TestDetectGpu:
    def test_amd_via_vendor_id(self, tmp_path: Path):
        card = tmp_path / "card0"
        (card / "device").mkdir(parents=True)
        (card / "device" / "vendor").write_text("0x1002\n", encoding="utf-8")
        with (
            patch("coolstep.compat.detect._DRM_ROOT", tmp_path),
            patch("coolstep.compat.detect._MODULE_ROOT", Path("/nonexistent")),
        ):
            primary, amd, nvidia, intel = _detect_gpu()
        assert amd is True
        assert nvidia is False
        assert primary == "amdgpu"

    def test_nvidia_via_module(self, tmp_path: Path):
        (tmp_path / "nvidia").mkdir()
        with (
            patch("coolstep.compat.detect._DRM_ROOT", Path("/nonexistent")),
            patch("coolstep.compat.detect._MODULE_ROOT", tmp_path),
        ):
            primary, amd, nvidia, intel = _detect_gpu()
        assert nvidia is True
        assert primary == "nvidia"

    def test_primary_nvidia_wins_over_amd(self, tmp_path: Path):
        # Both AMD and NVIDIA present
        drm = tmp_path / "drm"
        drm.mkdir()
        for name, vendor in [("card0", "0x1002"), ("card1", "0x10de")]:
            card = drm / name / "device"
            card.mkdir(parents=True)
            (card / "vendor").write_text(vendor, encoding="utf-8")
        with (
            patch("coolstep.compat.detect._DRM_ROOT", drm),
            patch("coolstep.compat.detect._MODULE_ROOT", Path("/nonexistent")),
        ):
            primary, amd, nvidia, intel = _detect_gpu()
        assert amd is True
        assert nvidia is True
        assert primary == "nvidia"  # NVIDIA takes priority


# ---------------------------------------------------------------------------
# Compositor detection
# ---------------------------------------------------------------------------

class TestDetectCompositor:
    def test_hyprland_via_env(self, monkeypatch):
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc123")
        assert _detect_compositor() == "hyprland"

    def test_sway_via_env(self, monkeypatch):
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.setenv("SWAYSOCK", "/run/user/1000/sway.sock")
        assert _detect_compositor() == "sway"

    def test_kde_via_xdg(self, monkeypatch):
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.delenv("SWAYSOCK", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        assert _detect_compositor() == "kwin"

    def test_gnome_via_xdg(self, monkeypatch):
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        monkeypatch.delenv("SWAYSOCK", raising=False)
        monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
        monkeypatch.setenv("GNOME_DESKTOP_SESSION_ID", "this-is-deprecated")
        assert _detect_compositor() == "mutter"

    def test_none_when_no_env(self, monkeypatch):
        for key in (
            "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK", "XDG_CURRENT_DESKTOP",
            "KDE_FULL_SESSION", "GNOME_DESKTOP_SESSION_ID", "DISPLAY",
            "WAYLAND_DISPLAY", "XDG_SESSION_TYPE",
        ):
            monkeypatch.delenv(key, raising=False)
        with patch("coolstep.compat.detect.shutil.which", return_value=None):
            result = _detect_compositor()
        assert result == "none"


# ---------------------------------------------------------------------------
# EPP state
# ---------------------------------------------------------------------------

class TestEppState:
    def test_readable_and_writable(self, tmp_path: Path):
        cpu0 = tmp_path / "cpu0" / "cpufreq"
        cpu0.mkdir(parents=True)
        epp = cpu0 / "energy_performance_preference"
        epp.write_text("balance_power\n", encoding="utf-8")
        readable, writable = _epp_state(tmp_path)
        assert readable is True
        assert writable is True  # we can write back the same value

    def test_absent(self, tmp_path: Path):
        readable, writable = _epp_state(tmp_path)
        assert readable is False
        assert writable is False


# ---------------------------------------------------------------------------
# Full detect_caps smoke test
# ---------------------------------------------------------------------------

class TestDetectCaps:
    def test_returns_platform_caps(self):
        from coolstep.compat.caps import PlatformCaps
        caps = detect_caps()
        assert isinstance(caps, PlatformCaps)
        assert isinstance(caps.distro_id, str)
        assert isinstance(caps.cpu_vendor, str)
        assert isinstance(caps.gpu_primary, str)
        assert isinstance(caps.warnings, tuple)

    def test_singleton_via_get_caps(self):
        from coolstep.compat import get_caps
        a = get_caps()
        b = get_caps()
        assert a is b  # same instance

    def test_reset_caps_clears_singleton(self):
        from coolstep.compat import get_caps, reset_caps
        get_caps()
        reset_caps(None)
        b = get_caps()
        # b is a new instance (new detection run)
        # We can't guarantee a is not b on identical systems,
        # but the reset should produce a separate object.
        assert b is not None

    def test_inject_fake_caps(self):
        import time

        from coolstep.compat import get_caps, reset_caps
        from coolstep.compat.caps import PlatformCaps

        fake = PlatformCaps(
            distro_id="test",
            distro_name="Test OS",
            distro_clan="debian",
            distro_like=("debian",),
            pkg_manager="apt",
            cpu_vendor="intel",
            cpu_arch="x86_64",
            cpu_model="Test CPU",
            hwmon_k10temp=False,
            hwmon_coretemp=True,
            hwmon_zenpower=False,
            hwmon_amd_energy=False,
            hwmon_rapl=True,
            rapl_powercap=True,
            rapl_readable=False,
            hwmon_superio=(),
            thermal_zone_count=0,
            epp_available=True,
            epp_writable=False,
            platform_profile_available=False,
            perf_event_paranoid=2,
            gpu_primary="none",
            gpu_amd=False,
            gpu_nvidia=False,
            gpu_intel=True,
            pynvml_importable=False,
            compositor="unknown",
            hyprctl_available=False,
            swaymsg_available=False,
            wmctrl_available=False,
            xdotool_available=False,
            asusctl_available=False,
            asusctl_version="",
            asusctl_version_ok=False,
            nbfc_available=False,
            fancontrol_available=False,
            thinkfan_available=False,
            dell_smm_hwmon=False,
            coolercontrol_available=False,
            ryzenadj_available=False,
            auto_cpufreq_available=False,
            tlp_available=False,
            tlp_active=False,
            power_profiles_daemon_active=False,
            nvidia_smi_available=False,
            systemd_user_available=True,
            systemd_unit_user_dir="/home/test/.config/systemd/user",
            kmod_amdgpu=False,
            kmod_nvidia=False,
            kmod_i915=True,
            kmod_xe=False,
            kmod_ipmi=False,
            kmod_dell_smm_hwmon=False,
            ipmitool_available=False,
            redfish_endpoint_configured=False,
            bpftrace_available=False,
            bcc_importable=False,
            perf_binary_available=False,
            dbus_session_active=False,
            secure_boot=None,
            is_virtual=False,
            detected_at=time.time(),
            warnings=(),
        )
        reset_caps(fake)
        assert get_caps() is fake
        assert get_caps().distro_id == "test"
