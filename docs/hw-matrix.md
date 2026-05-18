# Hardware compatibility matrix — emulation harness

> Companion to `platform-support-matrix.md`. While `platform-support-matrix`
> tracks **what coolstep does** on each platform, this file tracks **how
> coolstep is tested** against synthetic versions of those platforms.

## Why a synthetic harness instead of a VM lab

A VM farm covering 12 OS/CPU combinations would take ~60GB on pve, ~40 min
to provision, and seconds-to-minutes per CI run. The actual surface coolstep
reads from is small: `/proc/cpuinfo`, `/etc/os-release`, `/sys/class/hwmon`,
`/sys/class/drm`, `/sys/module`, `/sys/firmware`, plus a fixed set of
binaries on `PATH` and a fixed set of Python modules. We materialise that
surface in `tmp_path` per fixture, patch the few `Path` constants in
`coolstep.compat.detect`, and run `detect_caps()` end-to-end against the
fakeroot. Result: **<300ms for the full matrix**, CI-friendly, no kernel
quirks to chase.

The harness is `tests/compat/fixtures/_loader.py::fake_platform()` — see
its docstring for the snapshot DSL. Each snapshot is one Python file under
`tests/compat/fixtures/<name>/snapshot.py`.

## What we emulate

| Surface | Source of truth in real life | Faked via |
|---|---|---|
| `/proc/cpuinfo` | kernel | `snapshot.cpuinfo` (raw string) |
| `/etc/os-release` | distro packaging | `snapshot.os_release` |
| `/sys/class/hwmon/hwmon*/name` | hwmon drivers | `snapshot.hwmon` (list of dicts) |
| `/sys/class/drm/cardN/device/vendor` | DRM/PCI | `snapshot.drm_cards` |
| `/sys/module/<mod>` | kmod | `snapshot.modules` |
| `/sys/class/thermal/thermal_zone*` | ACPI thermal | `snapshot.thermal_zones` (count) |
| `/sys/devices/.../intel-rapl/energy_uj` | powercap | `snapshot.powercap_rapl` + `rapl_readable` |
| `/sys/devices/.../cpu0/cpufreq/energy_performance_preference` | intel_pstate / amd_pstate | `snapshot.epp` |
| `/sys/firmware/acpi/platform_profile` | ACPI platform-profile | `snapshot.platform_profile` |
| `/sys/firmware/efi/efivars/SecureBoot-*` | UEFI | `snapshot.secure_boot` + `efi_vars` |
| `/sys/class/dmi/id/{sys,product,chassis}_vendor` | DMI | `snapshot.dmi` |
| `/sys/class/power_supply/BAT*` | power_supply_class | `snapshot.power_supply` |
| `/run/user/<uid>/hypr/<sig>/.socket.sock` | Hyprland | `snapshot.hypr_socket` |
| Binaries on PATH (`asusctl`, `hyprctl`, …) | distro packaging | `snapshot.which` |
| `subprocess.run` outputs (asusctl --version, ryzenadj -i) | tools | `snapshot.subprocess` + `asusctl_version` |
| Importable Python modules (`pynvml`, `bcc`) | site-packages | `snapshot.py_modules` |
| `systemctl is-active <unit>` | systemd | `snapshot.subprocess` mapping |
| Environment (HYPRLAND_*, XDG_*, DBUS_*) | session/login | `snapshot.env` |

## Current snapshot inventory

| Snapshot | Representative real machine | Primary gotcha exercised |
|---|---|---|
| `asus_tuf_a15_7940hs` | ASUS TUF Gaming A15 FA507XV (target) | Dual-GPU 780M+4060M, full P2 actuator set, Hyprland session |
| `desktop_ryzen_7950x_rtx4090` | Custom AMD desktop, MSI MEG X670E | No platform_profile, KDE Wayland, ryzenadj missing → community pointer |
| `thinkpad_x1_carbon_i7_intel` | ThinkPad X1 Carbon Gen 11 | Intel iGPU only, `perf_event_paranoid=4` blocks eBPF, GNOME + thinkpad_acpi |
| `dell_poweredge_r750_server` | Dell PowerEdge R750 + iDRAC9 | facet=server, Redfish endpoint, RAPL unreadable, no compositor |
| `raspberry_pi_5_arm` | RPi 5 BCM2712 (Cortex-A76) | aarch64, no DMI, ACPI thermal zones, debian clan |

(Add 7 more from the parallel sub-agent task — list will grow.)

## What each test asserts

Two parametrized tests run per snapshot:

1. **`test_detect_caps_smoke`** — runs `detect_caps()` and verifies each
   field in `snapshot.expected` matches the corresponding `PlatformCaps`
   attribute. Missing attributes are reported as a failure list rather
   than first-failure abort, so a misconfigured snapshot reveals all its
   bad assertions at once.

2. **`test_install_plan_for_snapshot`** — runs
   `caps_to_install_plan(caps)` end-to-end:
   - Asserts the JSON serialiser doesn't blow up.
   - Asserts `plan.distro_*` / `pkg_manager` propagate from caps.
   - If `expected.expected_actuators` is set, asserts the active
     actuator list (excluding `readonly_log`) matches sorted.
   - If `expected.expected_community_pointer_ids` is set, asserts those
     pointer ids appear in the plan.

## Adding a new snapshot

1. Pick a real machine class. Capture its `/proc/cpuinfo` head, `/etc/os-release`, `lspci`, `ls /sys/class/hwmon/*/name`, `systemd-detect-virt`, `cat /sys/firmware/acpi/platform_profile` — that's enough to write the DSL.
2. Create `tests/compat/fixtures/<name>/snapshot.py` (and an empty `__init__.py`). Use an existing one as template.
3. Write `expected` dict with the caps fields you want to assert.
4. Run `pytest tests/compat/test_hw_matrix.py -k <name> -v`.
5. If your `expected` is wrong, fix it. If you find a real detect.py bug, fix that.

## Bug-case overlays

Real-world gotchas are tested via `tests/compat/interference/_scenarios.py`,
which **overlays** a base snapshot with a delta (env vars, replaced
`asusctl_version`, additional active services, etc.) without forking the
entire fixture. The interference suite (12 scenarios at time of writing)
covers conflicts between coolstep and other actors (PPD, TLP, game-mode,
secure-boot, two coolstep instances, Hyprland stale signature, …). See
`interference-matrix.md` for that catalogue — it is the **primary** pillar.
