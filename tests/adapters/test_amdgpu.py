"""amdgpu collector — fakeroot integration tests."""

from __future__ import annotations

from pathlib import Path

from coolstep.adapters.collectors.amdgpu import AmdGpuCollector


def _w(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_fake_amdgpu(root: Path, *, with_temp: bool = True) -> Path:
    drm = root / "class" / "drm"

    # card0 — amdgpu device
    card_dev = drm / "card0" / "device"
    _w(card_dev / "uevent", "DRIVER=amdgpu\nPCI_ID=1002:15BF\n")
    _w(card_dev / "product", "Radeon 780M Graphics")
    _w(
        card_dev / "pp_dpm_sclk",
        "0: 800Mhz\n1: 1300Mhz *\n2: 2700Mhz\n",
    )
    _w(
        card_dev / "pp_dpm_mclk",
        "0: 96Mhz\n1: 6400Mhz *\n",
    )
    _w(card_dev / "gpu_busy_percent", "23")
    if with_temp:
        _w(card_dev / "hwmon" / "hwmon0" / "temp1_input", "39000")
        _w(card_dev / "hwmon" / "hwmon0" / "power1_average", "26090000")
        _w(card_dev / "hwmon" / "hwmon0" / "in0_input", "1450")

    # card0-DP-1 — connector, должен пропуститься
    connector = drm / "card0-DP-1" / "device"
    _w(connector / "uevent", "DEVTYPE=drm_connector\n")

    return drm


def test_discover_with_amdgpu_card(tmp_path):
    drm = _build_fake_amdgpu(tmp_path)
    c = AmdGpuCollector(drm)
    assert c.discover()


def test_discover_skips_non_amdgpu(tmp_path):
    drm = tmp_path / "class" / "drm"
    card_dev = drm / "card0" / "device"
    _w(card_dev / "uevent", "DRIVER=nouveau\n")
    c = AmdGpuCollector(drm)
    assert not c.discover()


def test_sample_extracts_temp_power_voltage(tmp_path):
    drm = _build_fake_amdgpu(tmp_path)
    c = AmdGpuCollector(drm)
    assert c.discover()
    partial = c.sample()
    gpus = partial["gpus"]
    assert isinstance(gpus, list)
    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "Radeon 780M Graphics"
    assert g.temp_c == 39.0
    assert g.power_w == 26.09  # 26090000 μW → 26.09 W
    assert g.voltage_v == 1.45


def test_sample_extracts_current_dpm_clocks(tmp_path):
    drm = _build_fake_amdgpu(tmp_path)
    c = AmdGpuCollector(drm)
    assert c.discover()
    partial = c.sample()
    gpus = partial["gpus"]
    assert isinstance(gpus, list)
    assert gpus[0].sclk_mhz == 1300.0
    assert gpus[0].mclk_mhz == 6400.0


def test_sample_extracts_gpu_busy(tmp_path):
    drm = _build_fake_amdgpu(tmp_path)
    c = AmdGpuCollector(drm)
    assert c.discover()
    partial = c.sample()
    gpus = partial["gpus"]
    assert isinstance(gpus, list)
    assert gpus[0].util_pct == 23.0


def test_sample_handles_missing_hwmon(tmp_path):
    drm = _build_fake_amdgpu(tmp_path, with_temp=False)
    c = AmdGpuCollector(drm)
    assert c.discover()
    partial = c.sample()
    gpus = partial["gpus"]
    assert isinstance(gpus, list)
    assert gpus[0].temp_c is None
    assert gpus[0].power_w is None
    # sclk/mclk/util — всё ещё работают
    assert gpus[0].sclk_mhz == 1300.0


def test_make_returns_none_for_missing_drm():
    from coolstep.adapters.collectors import amdgpu

    # Создаём collector с заведомо отсутствующим drm_root
    collector = amdgpu.AmdGpuCollector(drm_root=Path("/nonexistent/drm"))
    assert collector.discover() is False
