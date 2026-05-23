# CLAUDE.md — `systemd/`

> User-level systemd unit files. Не system-level (никогда — coolstep живёт
> под user, не под root).

**Module version:** 0.1.0
**Last synced with master:** 2026-05-23
**Connectors:**
- ↑ master → `../CLAUDE.md`
- ← installed by → `Makefile:install-units`

## Active units

| File | What | Default port/path | MemoryMax | CPUQuota |
|---|---|---|---|---|
| `coolstep-collector.service` | Daemon main loop | — | 512M | 10% |
| `coolstep-dashboard.service` | FastAPI server | :18889 | 768M | 10% |
| `coolstep-bench-gc.service` | weekly bench/runs/ prune + index | (timer-driven) | 128M | 20% |
| `coolstep-bench-gc.timer` | Sun *-*-* 03:00 randomized 10min | — | — | — |

### Memory requirements — calibrated 2026-05-08

После 1h+ uptime в реальной нагрузке (14d telemetry ring + Lit tiles + SSE
fan-out) dashboard стабильно держится на **300-450MB**. При исходных
`MemoryMax=200M` процесс упирался и SSE/REST глохли в `OOM-pressure flapping`
(юзер видел `SSE disconnected — auto-reconnect`). Промежуточный
`MemoryHigh=400M` приводил к **24050× cgroup high reclaim hits за 5 минут**
— kernel непрерывно выдавливал страницы, обработчики не успевали отвечать.

Текущий потолок `MemoryMax=768M` без `MemoryHigh` — даёт workload расти
естественно до своих ~400MB и держит ~370MB запаса под всплески (drift
detector retraining, KNN bulk query, large /telemetry/range hit).

**Collector — calibrated 2026-05-11:** 31h pilot run на `MemoryMax=200M`
дал `memory.events.max = 868608` (~7.7 hits/sec): ring + ChromaDB query
path упирались в потолок постоянно, swap peak 307M, working set жался.
Bump до `MemoryMax=512M` (без `MemoryHigh`) — 2× от реального peak.
Если P1 ML predictor добавит inference state — пересмотреть до 768M.

**Если поднять P1 ML predictor (HDBSCAN + XGBoost online inference):**
пересмотреть. ML state в RAM может прибавить ещё 100-200MB → MemoryMax=1G.

### Actuator safety belt — added 2026-05-12 with P2.1 (revised same day)

`ExecStopPost=-%h/coolstep/systemd/coolstep-cleanup.sh` runs after the daemon
exits — including SIGKILL / OOM-kill where Python's `try/finally` revert
path doesn't fire. The script reads `data/runtime-state.json`:

- `armed_actions == []` (clean shutdown, daemon already reverted, OR never
  armed): **no-op**. Crucially: preserves user's manually-tuned asusctl
  fan-curve (e.g. quietify-mid 2026-05-07) across normal restarts.
- `armed_actions` non-empty (daemon died hard mid-bias): `asusctl fan-curve
  --mod-profile $COOLSTEP_ASUSCTL_PROFILE --default` then clears the list.

Earlier draft called `asusctl ... --default` unconditionally → wiped the
user's quietify-mid curve on every clean restart. Don't do that.

The daemon's own Python-level revert (`_revert_all_armed("shutdown")`) is
the **primary** path; the script is the second tier.

**Apply on running host:** `systemctl --user daemon-reload && systemctl
--user restart coolstep-collector.service`. Verify via `journalctl --user
-u coolstep-collector -n 5` after stop.

## Invariants

- **Type=simple.** Не forking, не notify. Daemon сам не форкает.
- **User-level only.** `systemctl --user`, `WantedBy=default.target`. Не
  `multi-user.target`. Reasoning: coolstep — личный процесс пользователя, не
  system service.
- **MemoryMax + CPUQuota гарантированы.** Это страховка против self-overload
  bug'ов. Если daemon начал жрать — systemd прибьёт.
- **ProtectHome=read-only** + `ReadWritePaths=%h/coolstep/data`. Daemon видит
  весь $HOME read-only кроме своего data/.
- **Никаких privileges.** No CAP_*, no AmbientCapabilities, no SupplementaryGroups.
  P0 actuator readonly — нет нужды. Для P2 hardware actuators — отдельно
  обсуждаем, что и в каком объёме.
- **Drop-in для tweaks.** Кастомные изменения user'а — `~/.config/systemd/
  user/coolstep-*.service.d/override.conf`, не править базовые unit'ы.
- **CPUAffinity check** — если добавляем `CPUAffinity=`, проверить что
  значение subset slice'а cpuset. См. memory feedback_drop_in_cpuset_check.md.

## Visit when

- меняешь port dashboard → этот файл (Active table) + dashboard/server.py default
- добавляешь slice (например `coolstep.slice` для отделения от user.slice) →
  координировать с zone_observer_slices в memory
- добавляешь новый unit → этот файл + Makefile install-units

## FAQ

**Q: Почему не auto-enable в pyproject?**
A: User должен явно `make enable-units` — systemd-side ввод опасен. Pyproject
ставит код, не сервисы.

**Q: Куда логи?**
A: `journalctl --user -u coolstep-collector` / `... coolstep-dashboard`. Mы
не пишем в файлы — Journal делает rotation сам.

**Q: Conflict с другими user-units?**
A: P0 — никакого. Когда P2 actuators появятся — `Conflicts=game-mode.service`?
Нет, лучше soft probe в коде. unit-level conflict жёсткий.

**Q: Почему `After=graphical-session.target` для collector?**
A: hyprctl collector требует HYPRLAND_INSTANCE_SIGNATURE. До graphical session
он не сработает. Если хочется headless → переменная WantsHeadless и пропуск
hyprctl в discovery.

## Sibling pointers

- → `../coolstep/daemon.py` — что запускается ExecStart'ом
- → `../coolstep/dashboard/server.py` — что запускает coolstep-dashboard
- → `../Makefile` — install-units / enable-units helpers
- See repo-internal notes (maintainer's local memory) for the broader slice/timer landscape.
