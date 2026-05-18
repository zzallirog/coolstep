"""Hyprland workload context collector.

Reads `hyprctl clients -j` (JSON list of windows). For each frame produces a
WorkloadFrame with top-level processes weighted by mapped+visible windows.
Falls back to silence (workload=None) on non-Hyprland hosts or when hyprctl
binary is missing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, ProcSig, SignalDescriptor, WorkloadFrame

log = logging.getLogger(__name__)

# `hyprctl clients -j` is a fork+exec+IPC + JSON parse; on a busy desktop it
# costs ~50-100 ms wall time — way over the per-collector budget (~10 ms).
# Window topology rarely changes within a second, so we cache the parsed
# WorkloadFrame and only re-shell every CACHE_SEC. Override via env if you
# want immediate workload reaction (debugging) or longer staleness.
CACHE_SEC = float(os.environ.get("COOLSTEP_HYPRCTL_CACHE_SEC", 5.0))
# Subprocess timeout default 2.0s — coordinated with `COLLECTOR_TIMEOUTS["hyprctl"]`
# in daemon.py. Old default 0.5s was too tight для cold-cache refresh на
# busy desktop (~95ms median, p99 >300ms — incident 2026-05-10 BLOCKER #1
# from collectors-audit). Override via env if нужно.
DEFAULT_TIMEOUT_S = float(os.environ.get("COOLSTEP_HYPRCTL_TIMEOUT_S", 2.0))


class HyprctlCollector:
    name = "hyprctl"

    def __init__(
        self,
        hyprctl_path: str = "hyprctl",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        runtime_dir: Path | None = None,
    ) -> None:
        self._hyprctl = hyprctl_path
        self._timeout = timeout_s
        self._cost = CostTracker()
        self._cached_workload: WorkloadFrame | None = None
        self._cached_at: float = 0.0
        # Track env-availability transitions. systemd may start the daemon
        # before Hyprland propagates HYPRLAND_INSTANCE_SIGNATURE через
        # `systemctl --user import-environment` (incident 2026-05-07). With
        # _env_alive state we re-arm whenever env reappears, no daemon
        # restart needed.
        self._env_alive: bool | None = None
        # Last signature мы реально стучались — для логгирования transition.
        self._last_sig: str | None = None
        self._runtime_dir = runtime_dir or Path(
            f"/run/user/{os.getuid()}/hypr"
        )

    def host_supported(self) -> bool:
        """Cheap check: is this host *capable* of running hyprctl?

        Used by `make()` для registry-time gate. Returns True iff the
        binary is on PATH — env var may transiently arrive later
        (systemctl import-environment race на user-session start).
        """
        return shutil.which(self._hyprctl) is not None

    def _live_signature(self) -> str | None:
        """Find a HYPRLAND signature with a реально-достижимый socket.

        Process env может содержать сигнатуру от уже-убитой Hyprland-сессии
        (long-running daemon-процесс родом из старой сессии — обнаружено
        2026-05-11 на dashboard: env=...1778276970..., live=...1778529776...).

        Стратегия:
          1. Если env-sig указывает на существующий `.socket.sock` → берём её.
          2. Иначе берём newest subdir of /run/user/<uid>/hypr/ с extant socket.

        Без stat() на socket это не «жив», а «есть на диске» — но Hyprland
        чистит свой runtime-dir при чистом exit; сирые dirs от crashed
        сессий могут остаться. Финальная проверка — фактический subprocess
        вызов в `_sample_inner` (он же убедится в живости).
        """
        env_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if env_sig:
            sock = self._runtime_dir / env_sig / ".socket.sock"
            if sock.exists():
                return env_sig
        if not self._runtime_dir.is_dir():
            return None
        candidates: list[tuple[float, str]] = []
        try:
            children = list(self._runtime_dir.iterdir())
        except OSError:
            return None
        for child in children:
            sock = child / ".socket.sock"
            try:
                mtime = sock.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, child.name))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def discover(self) -> bool:
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return False
        if shutil.which(self._hyprctl) is None:
            return False
        try:
            result = subprocess.run(
                [self._hyprctl, "version"],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="workload.top_processes", unit="-", dtype="list[ProcSig]",
                cardinality="per-device",
                source="hyprctl clients -j (mapped+visible windows)",
                requires=("hyprctl", "HYPRLAND_INSTANCE_SIGNATURE env"),
                description="Один ProcSig на уникальный win_class",
            ),
            SignalDescriptor(
                name="workload.rolling_features.visible_apps", unit="count",
                dtype="float", cardinality="scalar",
                source="hyprctl clients -j → count(mapped & !hidden)",
            ),
            SignalDescriptor(
                name="workload.rolling_features.unique_classes", unit="count",
                dtype="float", cardinality="scalar",
                source="hyprctl clients -j → distinct class",
            ),
        ]

    def sample(self) -> dict[str, object]:
        now = time.monotonic()
        # Late env propagation / stale env: переоткрываем живую Hyprland-сессию
        # на каждый sample. Может прийти не из env, а из /run/user runtime dir
        # — критично для long-running daemon'ов переживших Hyprland restart
        # (incident 2026-05-11: dashboard process с env от убитой сессии).
        live_sig = self._live_signature()
        env_alive = live_sig is not None
        if env_alive != self._env_alive:
            if env_alive:
                log.info(
                    "hyprctl: live Hyprland signature %s — going live", live_sig
                )
            elif self._env_alive is True:
                log.warning("hyprctl: no live Hyprland signature — going dark")
                self._cached_workload = None
                self._cached_at = 0.0
            self._env_alive = env_alive
        if live_sig is not None and live_sig != self._last_sig:
            # Switched к другой Hyprland-сессии — сбрасываем cache (window
            # topology старой сессии полностью невалидна).
            if self._last_sig is not None:
                log.info(
                    "hyprctl: signature changed %s → %s, dropping cache",
                    self._last_sig,
                    live_sig,
                )
                self._cached_workload = None
                self._cached_at = 0.0
            self._last_sig = live_sig
        if not env_alive:
            return {}

        if self._cached_workload is not None and (now - self._cached_at) < CACHE_SEC:
            with stopwatch() as sw:
                workload: WorkloadFrame | None = self._cached_workload
            self._cost.record_us(sw.elapsed_us)
            return {"workload": workload}
        with stopwatch() as sw:
            workload = self._sample_inner(live_sig)
        self._cost.record_us(sw.elapsed_us)
        if workload is not None:
            self._cached_workload = workload
            self._cached_at = now
            return {"workload": workload}
        # Subprocess flap: re-serve stale cache if available rather than going
        # dark in Chroma metadata (collectors-audit risk 4). После CACHE_SEC*3
        # окончательно сдаёмся и возвращаем None.
        if self._cached_workload is not None and (now - self._cached_at) < CACHE_SEC * 3:
            return {"workload": self._cached_workload}
        return {}

    def _sample_inner(self, live_sig: str | None = None) -> WorkloadFrame | None:
        # Inject live signature в env subprocess'а — даже если в env родителя
        # стоит мёртвая сигнатура. None → не override'им.
        env = None
        if live_sig is not None and os.environ.get(
            "HYPRLAND_INSTANCE_SIGNATURE"
        ) != live_sig:
            env = {**os.environ, "HYPRLAND_INSTANCE_SIGNATURE": live_sig}
        try:
            result = subprocess.run(
                [self._hyprctl, "clients", "-j"],
                capture_output=True,
                timeout=self._timeout,
                check=False,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.debug("hyprctl clients failed: %s", exc)
            return None
        if result.returncode != 0 or not result.stdout:
            return None
        try:
            clients = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(clients, list):
            return None
        return _windows_to_workload(clients)


def _windows_to_workload(clients: list[dict[str, object]]) -> WorkloadFrame:
    """Aggregate window list into a coarse WorkloadFrame.

    P0 — без обращения к /proc/<pid>/stat: Hyprland уже сообщает класс окна
    (`class`) и pid. cpu_pct/rss оставляем 0 — заполнятся в P1 кросс-collector
    merge'ом, либо отдельным psutil-collector'ом.
    """
    seen: dict[str, ProcSig] = {}
    visible_apps = 0
    fingerprint_features: dict[str, float] = {}
    # Track focused window (focusHistoryID=0 → most recently focused) для
    # label assignment. Без этого dominant class = первый-смотренный mapped,
    # что на 4 kitty + 1 steam всегда даёт 'kitty' даже когда юзер в Steam.
    focused_class: str | None = None
    focused_history_id = 99999

    for client in clients:
        if not isinstance(client, dict):
            continue
        win_class = str(client.get("class") or client.get("initialClass") or "unknown")
        pid = client.get("pid")
        mapped = bool(client.get("mapped", True))
        hidden = bool(client.get("hidden", False))
        if not mapped or hidden:
            continue
        visible_apps += 1
        if not isinstance(pid, int):
            continue
        if win_class not in seen:
            seen[win_class] = ProcSig(pid=pid, name=win_class, cpu_pct=0.0, rss_mb=0.0)
        fhid_raw = client.get("focusHistoryID")
        if isinstance(fhid_raw, int) and fhid_raw < focused_history_id:
            focused_history_id = fhid_raw
            focused_class = win_class

    fingerprint_features["visible_apps"] = float(visible_apps)
    fingerprint_features["unique_classes"] = float(len(seen))
    # Label — focused window class (focusHistoryID=0 — последний focused
    # в Hyprland). Это **активный** workload, не background tabs. Игры
    # обычно fullscreen+focused → label станет 'steam_app_...'/'gamescope'/
    # игровой class. Fallback на первый-mapped если focusHistoryID отсутствует
    # (не-Hyprland fork). P1 заменит на HDBSCAN cluster id.
    label = focused_class or (next(iter(seen)) if seen else None)
    return WorkloadFrame(
        top_processes=list(seen.values()),
        rolling_features=fingerprint_features,
        label=label,
    )


def make() -> HyprctlCollector | None:
    """Register collector on hosts where `hyprctl` binary is reachable.

    Env var `HYPRLAND_INSTANCE_SIGNATURE` is **not** required at registry
    time — it may be propagated to systemd user manager later via
    `systemctl --user import-environment` (race with Hyprland start,
    incident 2026-05-07). The collector self-recovers in `sample()` once
    the env appears.
    """
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not _c.hyprctl_available:
        return None
    collector = HyprctlCollector()
    return collector if collector.host_supported() else None
