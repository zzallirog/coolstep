"""Tests for coolstep.compat.install_plan."""

from __future__ import annotations

import time

from coolstep.compat.caps import PlatformCaps
from coolstep.compat.install_plan import caps_to_install_plan


def _make_caps(**overrides) -> PlatformCaps:
    """Build a minimal PlatformCaps for testing."""
    defaults = dict(
        distro_id="arch",
        distro_name="Arch Linux",
        distro_clan="arch",
        distro_like=(),
        pkg_manager="pacman",
        cpu_vendor="amd",
        cpu_arch="x86_64",
        cpu_model="Ryzen 9 7940HS",
        hwmon_k10temp=True,
        hwmon_coretemp=False,
        hwmon_zenpower=False,
        hwmon_amd_energy=False,
        hwmon_rapl=True,
        rapl_powercap=True,
        rapl_readable=False,
        hwmon_superio=(),
        thermal_zone_count=4,
        epp_available=True,
        epp_writable=True,
        platform_profile_available=True,
        perf_event_paranoid=2,
        gpu_primary="amdgpu",
        gpu_amd=True,
        gpu_nvidia=True,
        gpu_intel=False,
        pynvml_importable=True,
        compositor="hyprland",
        hyprctl_available=True,
        swaymsg_available=False,
        wmctrl_available=False,
        xdotool_available=False,
        asusctl_available=True,
        asusctl_version="6.1.2",
        asusctl_version_ok=True,
        nbfc_available=False,
        fancontrol_available=False,
        thinkfan_available=False,
        dell_smm_hwmon=False,
        coolercontrol_available=False,
        ryzenadj_available=True,
        auto_cpufreq_available=False,
        tlp_available=False,
        tlp_active=False,
        power_profiles_daemon_active=True,
        nvidia_smi_available=True,
        systemd_user_available=True,
        systemd_unit_user_dir="/home/user/.config/systemd/user",
        kmod_amdgpu=True,
        kmod_nvidia=True,
        kmod_i915=False,
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
    defaults.update(overrides)
    return PlatformCaps(**defaults)


class TestInstallPlan:
    def test_arch_full_setup(self):
        caps = _make_caps()
        plan = caps_to_install_plan(caps)
        assert plan.distro_id == "arch"
        assert plan.pkg_manager == "pacman"

        # All collectors should activate
        coll_names = {c.name for c in plan.collectors}
        assert "linux_sysfs" in coll_names
        assert "amdgpu" in coll_names
        assert "nvidia_nvml" in coll_names
        assert "hyprctl" in coll_names

        active_collectors = {c.name for c in plan.collectors if c.will_activate}
        assert "linux_sysfs" in active_collectors
        assert "amdgpu" in active_collectors

        # asusctl actuator active
        act_names = {a.name: a for a in plan.actuators}
        assert act_names["asusctl_fan_curve_bias"].will_activate is True
        assert act_names["epp_shift"].will_activate is True
        assert act_names["ryzenadj_cap"].will_activate is True

    def test_nvidia_without_pynvml_adds_warning(self):
        caps = _make_caps(gpu_nvidia=True, pynvml_importable=False)
        plan = caps_to_install_plan(caps)
        # nvidia_nvml should not activate
        nv = next(c for c in plan.collectors if c.name == "nvidia_nvml")
        assert nv.will_activate is False
        # pynvml should be in pip_required
        assert any("nvidia-ml-py" in p for p in plan.pip_required)

    def test_no_gpu_amd(self):
        caps = _make_caps(gpu_amd=False, gpu_primary="nvidia", kmod_amdgpu=False)
        plan = caps_to_install_plan(caps)
        amd = next(c for c in plan.collectors if c.name == "amdgpu")
        assert amd.will_activate is False
        assert "drm vendor" in amd.reason or "AMD GPU" in amd.reason

    def test_no_hyprctl(self):
        caps = _make_caps(hyprctl_available=False, compositor="kwin")
        plan = caps_to_install_plan(caps)
        hypr = next(c for c in plan.collectors if c.name == "hyprctl")
        assert hypr.will_activate is False

    def test_asusctl_old_version(self):
        caps = _make_caps(
            asusctl_available=True,
            asusctl_version="5.3.1",
            asusctl_version_ok=False,
        )
        plan = caps_to_install_plan(caps)
        act = next(a for a in plan.actuators if a.name == "asusctl_fan_curve_bias")
        assert act.will_activate is False
        assert "upgrade" in act.reason.lower() or "< 6" in act.reason

    def test_epp_not_writable(self):
        caps = _make_caps(epp_available=True, epp_writable=False)
        plan = caps_to_install_plan(caps)
        act = next(a for a in plan.actuators if a.name == "epp_shift")
        assert act.will_activate is False
        assert "not writable" in act.reason.lower()

    def test_summary_is_string(self):
        caps = _make_caps()
        plan = caps_to_install_plan(caps)
        summary = plan.summary()
        assert isinstance(summary, str)
        assert "coolstep" in summary.lower()

    def test_to_json_roundtrip(self):
        import json
        caps = _make_caps()
        plan = caps_to_install_plan(caps)
        raw = json.loads(plan.to_json())
        assert raw["distro_id"] == "arch"
        assert isinstance(raw["collectors"], list)

    def test_debian_no_asusctl_package(self):
        caps = _make_caps(
            distro_id="ubuntu",
            distro_name="Ubuntu 24.04",
            distro_clan="debian",
            pkg_manager="apt",
            asusctl_available=False,
        )
        plan = caps_to_install_plan(caps)
        # asusctl not in packages (not packaged for debian)
        all_pkgs = plan.packages_required + plan.packages_optional
        assert "asusctl" not in all_pkgs
        # Should have a note about building from source
        assert any("asusctl" in n.lower() for n in plan.notes)

    def test_systemd_units_always_present(self):
        caps = _make_caps()
        plan = caps_to_install_plan(caps)
        assert "coolstep-collector.service" in plan.systemd_units_to_enable
        assert "coolstep-dashboard.service" in plan.systemd_units_to_enable
