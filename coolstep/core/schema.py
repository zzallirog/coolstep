"""Platform-neutral telemetry contracts.

Adapters fill *partial* fields; the daemon merges per-tick. Strict mypy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Lookahead label for `was_hot_in_30s`. Backfilled once `now - lookahead > ts`;
# until then frames carry LABEL_UNKNOWN and KNN/where-filters skip them.
LABEL_UNKNOWN = -1
LABEL_COOL = 0
LABEL_HOT = 1


@dataclass(slots=True)
class CpuMetrics:
    freq_mhz: list[float] = field(default_factory=list)
    load_pct: list[float] = field(default_factory=list)
    # iowait_pct — per-core доля времени в состоянии iowait. Не входит в
    # load_pct: для thermal-load metric ядро в iowait = не исполняет
    # инструкции = не греется. Но workload classifier (P1) может различать
    # disk-bound и compute-bound фазы — отдельный сигнал даёт ему свободу.
    # Default — пустой list (P0 collector может не заполнять).
    iowait_pct: list[float] = field(default_factory=list)
    temps_c: dict[str, float] = field(default_factory=dict)
    voltage_v: dict[str, float] = field(default_factory=dict)
    power_w: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class GpuMetrics:
    name: str
    temp_c: float | None = None
    power_w: float | None = None
    sclk_mhz: float | None = None
    mclk_mhz: float | None = None
    util_pct: float | None = None
    voltage_v: float | None = None


@dataclass(slots=True)
class FanMetrics:
    name: str
    rpm: int | None = None
    pwm: int | None = None


@dataclass(slots=True)
class ProcSig:
    pid: int
    name: str
    cpu_pct: float
    rss_mb: float
    io_read_kb: float = 0.0
    io_write_kb: float = 0.0
    gpu_pct: float | None = None


@dataclass(slots=True)
class WorkloadFrame:
    top_processes: list[ProcSig] = field(default_factory=list)
    rolling_features: dict[str, float] = field(default_factory=dict)
    label: str | None = None


@dataclass(slots=True)
class TelemetryFrame:
    timestamp: float
    cpu: CpuMetrics = field(default_factory=CpuMetrics)
    gpus: list[GpuMetrics] = field(default_factory=list)
    fans: list[FanMetrics] = field(default_factory=list)
    storage_temps_c: dict[str, float] = field(default_factory=dict)
    memory_temps_c: dict[str, float] = field(default_factory=dict)
    workload: WorkloadFrame | None = None
    platform_state: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Cost:
    sample_us: int
    rss_kb: int


@dataclass(slots=True, frozen=True)
class SignalDescriptor:
    """Zabbix-LLD-style signal declaration.

    Each collector declares a list of these via `signals()`. The dashboard's
    «Discovered signals» tile and research tools read these instead of hard-coding
    field names.
    """

    name: str                   # e.g. "cpu.tctl"
    unit: str                   # "°C", "MHz", "RPM", "W", "V", "%"
    dtype: str                  # "float", "int", "list[float]", "dict[str, float]"
    source: str                 # e.g. "/sys/class/hwmon/hwmonN/temp1_input"
    description: str = ""
    cardinality: str = "scalar" # "scalar" | "per-core" | "per-device" | "dict"
    requires: tuple[str, ...] = ()  # external deps: kernel module, command, env


class ActionVerb(StrEnum):
    RAMP_COOLING = "ramp_cooling"
    CAP_BOOST = "cap_boost"
    SHIFT_POWER_ENVELOPE = "shift_power_envelope"
    DEFER_WORKLOAD = "defer_workload"
    NOTIFY_USER = "notify_user"
    # P2.3 — quiet-mode counterpart of RAMP_COOLING. Emitted when the
    # predictor is confidently calm AND ambient telemetry is safely below
    # the thermal knee: actuators *subtract* from the fan curve (or lower
    # boost ceiling gently) to reduce noise. Safety belt is enforced both
    # in decision.py (won't emit if hot/uncertain) and in the actuator
    # (revert on Tctl > safety ceiling).
    REDUCE_NOISE = "reduce_noise"


@dataclass(slots=True)
class Action:
    verb: ActionVerb
    # P2.4 — opened to `object` so adaptive verbs can carry a nested
    # `curve_context` dict alongside the scalar params. Earlier callers
    # that wrote only float/int/str continue to work; new callers may
    # write structured payloads (e.g. an Action's `curve_context`).
    params: dict[str, object]
    expires_at: float


@dataclass(slots=True)
class SimResult:
    expected_effect: dict[str, float]
    confidence: float
    reverts_in: float


@dataclass(slots=True)
class ActionResult:
    applied_at: float
    cmd_executed: str | None
    stdout_tail: str = ""
    error: str | None = None


def merge_partial(frame: TelemetryFrame, partial: dict[str, object]) -> TelemetryFrame:
    """Apply a collector's partial sample to an existing TelemetryFrame.

    Collectors return dicts whose keys map to TelemetryFrame attributes. Lists
    are extended; dicts are updated; scalars are overwritten when not None.
    """
    cpu_partial = partial.get("cpu")
    if isinstance(cpu_partial, dict):
        for list_field in ("freq_mhz", "load_pct", "iowait_pct"):
            if list_field in cpu_partial:
                setattr(frame.cpu, list_field, list(cpu_partial[list_field]))  # type: ignore[arg-type]
        for bucket_name in ("temps_c", "voltage_v", "power_w"):
            bucket_val = cpu_partial.get(bucket_name)
            if isinstance(bucket_val, dict):
                getattr(frame.cpu, bucket_name).update(bucket_val)

    gpus_partial = partial.get("gpus")
    if isinstance(gpus_partial, list):
        frame.gpus.extend(g for g in gpus_partial if isinstance(g, GpuMetrics))

    fans_partial = partial.get("fans")
    if isinstance(fans_partial, list):
        frame.fans.extend(f for f in fans_partial if isinstance(f, FanMetrics))

    for bucket_name in ("storage_temps_c", "memory_temps_c", "platform_state"):
        bucket_val = partial.get(bucket_name)
        if isinstance(bucket_val, dict):
            getattr(frame, bucket_name).update(bucket_val)

    workload_partial = partial.get("workload")
    if isinstance(workload_partial, WorkloadFrame):
        frame.workload = workload_partial

    return frame
