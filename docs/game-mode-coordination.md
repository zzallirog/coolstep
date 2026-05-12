# Game-mode coordination

> Cross-ref: ADR-015 (planned), `coolstep/adapters/actuators/asusctl_fan_curve.py`,
> `~/bin/game-mode-watcher`, `feedback_steam_wrapper_race.md`,
> `feedback_game_mode_bypass_audit.md`.

## Why this exists

`game-mode.service` (via `game-mode-watcher.service` + Steam wrapper at `~/bin/steam`)
already does a lot when a game session starts:

- switches ASUS platform profile to *Performance*
- applies its own fan curve tuning
- re-nices game processes, sets CPU governor, pins frequencies

coolstep's `AsusctlFanCurve` actuator does *similar things differently* — it
biases the active fan curve upward in the 70-85°C knee band. Running both
simultaneously leads to unpredictable interactions: the two tools may overwrite
each other's fan-curve writes, making it impossible to debug which one is
actually responsible for a thermal event.

The guiding principle here is **«не наступать на game-mode.service»** —
coolstep is a *predictive* pre-heat layer, not a replacement for mode
orchestration. During a gaming session game-mode is already orchestrating;
coolstep should stay out of the way.

## Default behaviour — `COOLSTEP_GAME_MODE_DEFER=1`

With the default (or explicit `COOLSTEP_GAME_MODE_DEFER=1`),
`AsusctlFanCurve.supports(RAMP_COOLING)` returns `False` while
`game-mode.service` is active.

What this means in practice:

1. The KNN predictor continues collecting telemetry and emitting predictions
   as normal.
2. The decision engine generates `RAMP_COOLING` `Action` objects as normal.
3. The daemon's actuator router calls `supports()` — gets `False` — and drops
   the action silently (per `actuators/CLAUDE.md` FAQ «А если все actuators
   скажут supports=False? Action молча drop'ается»).
4. No `asusctl fan-curve --data ...` command is ever issued. The journal shows
   no `apply` events during the gaming session.

This is the safe, zero-conflict path.

## Cooperative mode — `COOLSTEP_GAME_MODE_DEFER=0`

Setting `COOLSTEP_GAME_MODE_DEFER=0` flips to cooperative mode:
`supports()` returns `True` even when game-mode is active. coolstep will stack
its predictive bias on top of game-mode's curve.

Use case: deliberate stress-test runs (scenario S3 in the stress harness) where
you want to measure how coolstep's bias interacts with game-mode's own profile.

**Risks:**

- The two tools are now writing to the same asusctl profile. Order of writes
  determines who wins at any given moment — this is hard to trace in journals.
- Harder to attribute thermal outcomes: did the bias help, or was it game-mode
  that actually moved the fan?
- Not recommended for production gaming sessions; only for harness-controlled
  experiments.

```bash
# Enable cooperative mode for one session:
COOLSTEP_GAME_MODE_DEFER=0 systemctl --user restart coolstep-collector.service

# Back to default:
systemctl --user restart coolstep-collector.service   # unset = defer
```

## Live probe — 5s cache

Every time the actuator's `supports()` is called, it checks whether
`game-mode.service` is active. To keep this cheap (the daemon ticks every few
seconds), the result is cached for **5 seconds** using `time.monotonic()`.

Implementation details:

- `subprocess.run(["systemctl", "--user", "is-active", "game-mode.service"], timeout=0.5)`
- Returns `True` iff `returncode == 0` AND `stdout.strip() == b"active"`.
- Any exception (timeout, OSError, missing unit) returns `False` — game-mode
  probe failure never blocks coolstep.
- Cache stored in `self._gm_cache: tuple[bool, float] | None`. Refreshed when
  the cached timestamp is more than `_GM_CACHE_TTL_S = 5.0` seconds old.
- When the state *flips to active*, a `log.debug(...)` is emitted once per
  cache window: `"game-mode active, supports() -> False (deferring)"`.

Probe cost: < 5 ms on a healthy system; zero overhead inside the 5s window.

## Migration to P2.5 (ADR-015)

The planned `game_mode_optimizer.py` actuator (ADR-015) will become the
**sole** actuator that fires when game-mode is active. It will own the
Performance profile fan curve entirely during gaming sessions, applying
coolstep's predictive bias in a game-mode-aware way (e.g., higher ceiling,
different knee band tuning for sustained GPU + CPU load).

When P2.5 ships:

- `AsusctlFanCurve` continues to default to `DEFER=1` (this defer logic stays
  as a kill switch).
- `game_mode_optimizer.py` will own `supports()` returning `True` *only while*
  game-mode is active; `AsusctlFanCurve.supports()` will return `False` in that
  window.
- The two actuators are mutually exclusive by design: no new env flag needed,
  the presence of `game_mode_optimizer` makes the defer flag redundant (but it
  stays for emergencies).

The `COOLSTEP_GAME_MODE_DEFER` env flag is a **transitional kill switch**, not
a long-term API. After P2.5 ships and is validated, the defer check may be
removed from `AsusctlFanCurve` in a clean-up commit.

## Verify

Check what coolstep currently sees:

```bash
# Is game-mode active right now?
systemctl --user is-active game-mode.service

# What coolstep logged during the last gaming session:
journalctl --user -u coolstep-collector | grep "game-mode"

# Confirm no apply events during a gaming session (DEFER=1):
journalctl --user -u coolstep-collector | grep "APPLIED\|DRY-RUN"
```

During a game session with `DEFER=1` you should see no `APPLIED` lines and
exactly zero `DRY-RUN` lines from `asusctl_fan_curve_bias` while game-mode is
active.

## P2.5 — `game_mode_optimizer` actuator (2026-05-12)

When `game-mode.service` is active AND defer-mode is on
(`COOLSTEP_GAME_MODE_DEFER=1`, default), `asusctl_fan_curve_bias.supports()`
returns `False` AND `game_mode_optimizer.supports()` returns `True`. They're
mutually exclusive — exactly one (or zero, under cooperative) actuator
handles `RAMP_COOLING` at any given moment.

Truth table for `RAMP_COOLING` (other verbs are always `False`):

| game-mode | DEFER     | `asusctl_fan_curve_bias` | `game_mode_optimizer` |
|-----------|-----------|--------------------------|-----------------------|
| inactive  | any       | True                     | False                 |
| active    | 1 (default) | False                  | True                  |
| active    | 0 (cooperative) | True               | False                 |

Cooperative mode (`COOLSTEP_GAME_MODE_DEFER=0`): `asusctl_fan_curve_bias`
owns, `game_mode_optimizer` stays silent. Useful for stress-test runs that
want KNN-driven bias stacked on top of game-mode's own profile.

Game-profile curve (`GAME_PROFILE_ANCHORS` in
`coolstep/adapters/actuators/game_mode_optimizer.py`) — more aggressive than
quietify-mid in the 73-78°C band, matching a Steam workload heat signature.
Empirically -3°C peak Tctl vs Performance default.

Anchors (temp:pwm):

    (60, 5), (65, 15), (70, 30), (73, 50),
    (76, 70), (80, 85), (85, 95), (90, 100)

P2.5 is the minimal first version — KNN doesn't drive intensity here.
Game-mode itself is the strongest predictor; the actuator biases up
unconditionally while game-mode is on, reverts to asusctl `--default` when
game-mode goes inactive (either via TTL or detected via the `supports()`
flip on the next tick).
