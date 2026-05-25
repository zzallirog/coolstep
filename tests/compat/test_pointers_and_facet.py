"""Tests for T4 (community pointers) and T5 (caps.facet + install_plan facet)."""

from __future__ import annotations

import time
from typing import Any

from coolstep.compat.caps import PlatformCaps
from coolstep.compat.install_plan import caps_to_install_plan


def _caps(**overrides: Any) -> PlatformCaps:
    defaults = dict(
        distro_id="arch", distro_name="Arch Linux", distro_clan="arch",
        distro_like=(), pkg_manager="pacman",
        cpu_vendor="amd", cpu_arch="x86_64", cpu_model="Ryzen 9 7940HS",
        hwmon_k10temp=True, hwmon_coretemp=False, hwmon_zenpower=False,
        hwmon_amd_energy=False, hwmon_rapl=True,
        rapl_powercap=True, rapl_readable=False,
        hwmon_superio=(), thermal_zone_count=4,
        epp_available=True, epp_writable=True, platform_profile_available=True,
        perf_event_paranoid=2,
        gpu_primary="amdgpu", gpu_amd=True, gpu_nvidia=False, gpu_intel=False,
        pynvml_importable=False,
        compositor="hyprland",
        hyprctl_available=True, swaymsg_available=False,
        wmctrl_available=False, xdotool_available=False,
        asusctl_available=True, asusctl_version="6.3.6", asusctl_version_ok=True,
        nbfc_available=False, fancontrol_available=False, thinkfan_available=False,
        dell_smm_hwmon=False, coolercontrol_available=False,
        ryzenadj_available=True, auto_cpufreq_available=False,
        tlp_available=False, tlp_active=False,
        power_profiles_daemon_active=True, nvidia_smi_available=False,
        systemd_user_available=True, systemd_unit_user_dir="/home/u/.config/systemd/user",
        kmod_amdgpu=True, kmod_nvidia=False, kmod_i915=False, kmod_xe=False,
        kmod_ipmi=False, kmod_dell_smm_hwmon=False,
        ipmitool_available=False, redfish_endpoint_configured=False,
        bpftrace_available=False, bcc_importable=False,
        perf_binary_available=True, dbus_session_active=False,
        secure_boot=None, is_virtual=False,
        detected_at=time.time(), warnings=(),
    )
    defaults.update(overrides)
    return PlatformCaps(**defaults)


class TestFacetDetection:
    def test_laptop_with_battery_is_desktop(self):
        c = _caps(platform_profile_available=True, epp_available=True)
        assert c.facet == "desktop"

    def test_rack_server_with_ipmi_kmod(self):
        c = _caps(
            kmod_ipmi=True,
            ipmitool_available=True,
            platform_profile_available=False,
            epp_available=False,
            compositor="none",
        )
        assert c.facet == "server"

    def test_cloud_vm_is_server(self):
        c = _caps(
            is_virtual=True,
            platform_profile_available=False,
            epp_available=False,
            compositor="none",
            hyprctl_available=False,
        )
        assert c.facet == "server"

    def test_redfish_endpoint_forces_server(self):
        c = _caps(redfish_endpoint_configured=True)
        assert c.facet == "server"

    def test_headless_host_without_laptop_profile_is_server(self):
        # Headless host with no compositor and no consumer platform_profile
        # is treated as server-like — EPP alone is no longer a laptop signal
        # because modern Xeon / EPYC also expose energy_performance_preference.
        c = _caps(
            platform_profile_available=False,
            epp_available=True,
            compositor="none",
            hyprctl_available=False,
            kmod_ipmi=False,
            ipmitool_available=False,
            is_virtual=False,
        )
        assert c.facet == "server"


class TestFacetOverride:
    def test_default_uses_caps_facet(self):
        c = _caps()
        plan = caps_to_install_plan(c)
        assert plan.facet == "desktop"

    def test_override_to_server(self):
        c = _caps()
        plan = caps_to_install_plan(c, facet_override="server")
        assert plan.facet == "server"
        # Server preset → actuator disable note
        assert plan.env_suggestions.get("COOLSTEP_ACTUATOR_ENABLE") == "false"
        assert plan.env_suggestions.get("COOLSTEP_FACET") == "server"

    def test_override_to_desktop(self):
        c = _caps()
        plan = caps_to_install_plan(c, facet_override="desktop")
        assert plan.facet == "desktop"
        assert plan.env_suggestions.get("COOLSTEP_FACET") == "desktop"


class TestCommunityPointers:
    def test_perf_paranoid_locked(self):
        c = _caps(perf_event_paranoid=3, perf_binary_available=True)
        plan = caps_to_install_plan(c)
        triggers = [p.trigger for p in plan.community_pointers]
        assert any("paranoid" in t.lower() for t in triggers)

    def test_rapl_locked_pointer(self):
        c = _caps(rapl_powercap=True, rapl_readable=False)
        plan = caps_to_install_plan(c)
        assert any("RAPL" in p.trigger for p in plan.community_pointers)

    def test_no_bpftrace_pointer(self):
        c = _caps(bpftrace_available=False, bcc_importable=False)
        plan = caps_to_install_plan(c)
        assert any("bpftrace" in p.trigger for p in plan.community_pointers)

    def test_server_without_ipmi_gets_pointer(self):
        c = _caps(
            ipmitool_available=False, kmod_ipmi=True,
            platform_profile_available=False, epp_available=False,
        )
        # Force server facet
        plan = caps_to_install_plan(c, facet_override="server")
        assert any("ipmitool" in p.trigger.lower() or "Server-class" in p.trigger
                   for p in plan.community_pointers)

    def test_pointer_command_per_distro(self):
        c = _caps(distro_clan="debian", pkg_manager="apt",
                  bpftrace_available=False)
        plan = caps_to_install_plan(c)
        bpftrace_p = next(p for p in plan.community_pointers if "bpftrace" in p.trigger)
        assert "apt" in bpftrace_p.commands["debian"]

    def test_summary_includes_pointers_section(self):
        c = _caps(perf_event_paranoid=3)
        plan = caps_to_install_plan(c)
        summary = plan.summary()
        assert "Community drivers" in summary
        assert "Deployment target:" in summary
