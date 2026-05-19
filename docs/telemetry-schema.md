# Telemetry schema

> What each collector emits, and how the pieces compose into one frame
> per tick.

## The unified frame

Regardless of which adapters are active on a given host, the daemon
assembles exactly one `TelemetryFrame` per tick:

```python
@dataclass
class TelemetryFrame:
    timestamp: float
    cpu: CpuMetrics                # freq, load, iowait, temps, voltage, power
    gpus: list[GpuMetrics]         # one entry per discovered GPU
    fans: list[FanMetrics]         # rpm + pwm
    storage_temps_c: dict[str, float]
    memory_temps_c: dict[str, float]
    workload: WorkloadFrame | None # focused app + top processes
    platform_state: dict[str, str] # governor, EPP, platform_profile, …
```

`CpuMetrics` itself is the densest:

```python
@dataclass
class CpuMetrics:
    freq_mhz:   list[float]            # per-core, current frequency
    load_pct:   list[float]            # per-core, delta-based from /proc/stat
    iowait_pct: list[float]            # per-core iowait
    temps_c:    dict[str, float]       # tctl, tdie, package, per-core, …
    voltage_v:  dict[str, float]       # vcore, vsoc, …
    power_w:    dict[str, float]       # pkg, dram, cores, psu_total, …
```

The schema is a strict superset of what any single collector returns.
Each collector fills its slice; `merge_partial()` in `core.schema`
combines them. Missing slices stay empty — the predictor's embedder
handles absent fields with zero-imputation, and the dashboard's tiles
fall back to "—" for any value the host can't see.

## Who fills what

The mapping below is the contract. New collectors must declare their
contributions in their `signals()` manifest, so the dashboard's
"discovered signals" tile reflects reality without code changes.

### `linux_sysfs`

The general-purpose Linux collector. Reads `/sys/class/hwmon/*`,
`/sys/devices/system/cpu/cpu*/cpufreq/`, `/proc/stat`,
`/sys/firmware/acpi/platform_profile`.

| Field | Source | Notes |
|---|---|---|
| `cpu.freq_mhz[i]` | `cpu{i}/cpufreq/scaling_cur_freq` | Per-core; selective 3-tick refresh |
| `cpu.load_pct[i]` | `/proc/stat` delta | Per-core; computed from sample-to-sample delta |
| `cpu.iowait_pct[i]` | `/proc/stat` delta | Separate from `load_pct` |
| `cpu.temps_c["tctl"]` | k10temp / coretemp hwmon | Reports tctl/tdie/package based on driver |
| `cpu.voltage_v[*]` | hwmon `in*_input` | Voltages from k10temp / asus drivers |
| `fans[*]` | hwmon `fan*_input` + `pwm*` | All discovered fans |
| `storage_temps_c[*]` | nvme / drivetemp hwmon | NVMe and SATA drive temps |
| `memory_temps_c[*]` | spd5118 / jc42 hwmon | DDR5 module temps where available |
| `platform_state["governor"]` | `cpufreq/scaling_governor` | Performance / powersave / schedutil |
| `platform_state["epp"]` | `cpufreq/energy_performance_preference` | Intel / AMD EPP setting |
| `platform_state["platform_profile"]` | `/sys/firmware/acpi/platform_profile` | Quiet / Balanced / Performance |

The hwmon name set that decides which devices belong to which bucket is
loaded from `coolstep/compat/core.json:hwmon` and is overridable via
the L1 / L2 manifest layers.

### `amdgpu`

For each `/sys/class/drm/card*/device/vendor == 0x1002`:

| Field | Source |
|---|---|
| `gpus[*].name` | `"amdgpu_card{n}"` |
| `gpus[*].temp_c` | hwmon under `device/hwmon/hwmon*/` |
| `gpus[*].power_w` | same, `power1_average` |
| `gpus[*].sclk_mhz` | `pp_dpm_sclk` (regex for current marker `*`) |
| `gpus[*].mclk_mhz` | `pp_dpm_mclk` |
| `gpus[*].voltage_v` | hwmon `in*_input` |

### `nvidia_nvml`

Via `pynvml` (`nvidia-ml-py`). One `GpuMetrics` entry per device:

| Field | NVML call |
|---|---|
| `gpus[*].temp_c` | `nvmlDeviceGetTemperature(GPU)` |
| `gpus[*].power_w` | `nvmlDeviceGetPowerUsage / 1000` |
| `gpus[*].sclk_mhz` | `nvmlDeviceGetClockInfo(GRAPHICS)` |
| `gpus[*].mclk_mhz` | `nvmlDeviceGetClockInfo(MEM)` |
| `gpus[*].util_pct` | `nvmlDeviceGetUtilizationRates().gpu` |

### `intel_i915`

For each Intel DRM card (`vendor == 0x8086`):

| Field | Source |
|---|---|
| `gpus[*].name` | `"intel_card{n}"` |
| `gpus[*].temp_c` | `device/hwmon/hwmon*/temp1_input` |
| `gpus[*].power_w` | `device/hwmon/hwmon*/power1_input` |
| `gpus[*].sclk_mhz` | `gt/gt0/rps_act_freq_mhz` (with fallback to `gt_act_freq_mhz`) |
| `platform_state["intel_throttle.*"]` | `gt/gt0/throttle_reason_*` flags |

### `rapl_energy`

Reads `/sys/class/powercap/intel-rapl/*/energy_uj` (works on Intel and
modern AMD — the path name is historical). Delta-based: the first sample
primes state, subsequent samples produce power values:

| Field | Domain |
|---|---|
| `cpu.power_w["pkg"]` | package-0 |
| `cpu.power_w["pkg-1"]` | package-1 (dual-socket) |
| `cpu.power_w["dram"]` | DRAM subdomain |
| `cpu.power_w["cores"]` | core subdomain |
| `cpu.power_w["uncore"]` | uncore subdomain |
| `cpu.power_w["psys"]` | platform-wide (rare) |

`energy_uj` is root-only on post-CVE-2020-8694 kernels. The collector
still registers when readability fails and surfaces a community pointer
for granting `CAP_SYS_ADMIN`.

### `arm_thermal`

Reads `/sys/class/thermal/thermal_zone*`. Standard ACPI; useful on ARM
(Graviton, Ampere, Raspberry Pi, Apple Silicon under Asahi) and as a
fallback on x86 hosts without hwmon drivers:

| Field | Source |
|---|---|
| `cpu.temps_c["cpu-thermal"]` | thermal zones of type `cpu-thermal` |
| `cpu.temps_c["x86_pkg_temp"]` | thermal zones of type `x86_pkg_temp` |
| `cpu.temps_c["acpitz"]` | generic ACPI thermal zones |

### `hyprctl`, `dbus_session`

Workload context. Whichever one fires (`hyprctl` on Hyprland,
`dbus_session` on KDE/GNOME via `gdbus`), they fill the same field:

| Field | Source |
|---|---|
| `workload.label` | Focused window's resource class |
| `workload.top_processes[*]` | Mapped/visible apps |
| `workload.rolling_features["visible_apps"]` | Count of mapped windows |

### `redfish`

For rack servers with a BMC. Env-driven config
(`COOLSTEP_REDFISH_URL`, `_USER`, `_PASS`). Stdlib HTTPS, no
extra deps. Reads `/redfish/v1/Chassis/{ID}/Thermal` and `/Power`:

| Field | Redfish path |
|---|---|
| `fans[*]` | `Thermal#Fans[*].Reading` |
| `cpu.temps_c["inlet"]` | `Thermal#Temperatures[Name~"Inlet"]` |
| `cpu.temps_c["exhaust"]` | `Thermal#Temperatures[Name~"Exhaust"]` |
| `cpu.temps_c["cpu_max"]` | max of `Thermal#Temperatures[Name~"CPU"]` |
| `cpu.power_w["psu_total"]` | sum of `Power#PowerSupplies[*].PowerOutputWatts` |

### `ipmi`

Fallback for hosts where `ipmitool` is the only BMC interface. Parses
`ipmitool sensor` output. Same target fields as `redfish`.

### `perf_events`

Long-running `perf stat -e cycles,instructions,cache-misses,cache-references,task-clock -I 1000 -x , -a`
subprocess + background reader thread. Hardware-counter signals appear
in `platform_state` as compact strings:

| Key | Meaning |
|---|---|
| `perf.pmu.cycles` | Cycles per second |
| `perf.pmu.instructions` | Retired instructions per second |
| `perf.pmu.cache_misses` | LLC miss rate |
| `perf.pmu.cpi` | Cycles per instruction (derived) |
| `perf.pmu.cache_miss_pct` | Miss / reference percent (derived) |

### `ebpf_sched`

`bpftrace -f json` subprocess reading scheduler tracepoints:

| Key | Meaning |
|---|---|
| `ebpf.ctx_switch_max` | Max per-CPU switch rate this second |
| `ebpf.ctx_switch_total` | Aggregate switch rate |
| `ebpf.fork_rate` | `sched_process_fork` events per second |
| `ebpf.exec_rate` | `sched_process_exec` events per second |

## Persistence

The SQLite store keeps shortcut columns for the most-queried fields
(`cpu_temp` = max of `cpu.temps_c`, `cpu_power` = `cpu.power_w["pkg"]`,
`gpu_temp` = max across `gpus[*].temp_c`, etc.) so the dashboard's
sparkline queries are fast. The full frame is also stored as a JSON
blob for forensics — `coolstep export` and `coolstep export-telemetry`
both read from the blob, not the shortcut columns.

## See also

- [architecture.md](architecture.md) — how the schema fits into the
  tick loop
- [calibration-gates.md](calibration-gates.md) — which signals are
  required before the predictor can promote from dry-run to armed
