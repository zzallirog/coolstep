# bench/ — coolstep actuator stress-test harness (P2.3)

Captures telemetry + ML-state during synthetic load scenarios, then computes
the **metric of record**: did coolstep predict thermal pressure *before* fans
ramped up? Target delta ≥ 8 s positive.

## Quickstart

```bash
# 2-min CPU burst, dry-run (default), sampler active
./bench/stress.sh S1

# 90-s mixed compute+iowait
./bench/stress.sh S2

# 5-second smoke (ci/dev)
./bench/stress.sh S1 --duration 5
```

Output lands in `bench/runs/<ts>-<scenario>/` with three files:
- `telemetry.jsonl` — one JSON/line: `/api/telemetry/latest` snapshot + `recorded_at`
- `ml-state.jsonl` — one JSON/line: `data/ml-state.json` snapshot + `recorded_at`
- `meta.json` — scenario, armed, duration, Hyprland signature start/end

## Scenarios

| ID | Load | Default duration | Purpose |
|----|------|-----------------|---------|
| S1 | 16-thread 100% CPU | 120 s | Baseline compute burst |
| S2 | 8-thread CPU + 4 HDD workers | 90 s | Mixed compute + iowait |
| S3 | 16-thread 80% CPU | 1800 s | 30-min heat-soak |
| S4 | User-driven (no synthetic) | 300 s | Real workload (Steam, compile, …) |

## Analysis

```bash
python3 bench/analyze.py bench/runs/<ts>-<scenario>/
```

Prints: peak Tctl, time ≥ 85 °C, first `throttle_prob ≥ 0.65` timestamp,
first fan rampup ≥ 4500 RPM timestamp, delta, and an ASCII sparkline of
cpu_temp over the run.

## Armed mode

By default the harness is **dry-run** — actuators log commands but do not
execute. To enable real actuator firing:

```bash
./bench/stress.sh S1 --armed
```

This exports `COOLSTEP_ACTUATOR_ENABLE=true` for the duration of the run.
The daemon must be restarted with `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1`
set in its environment first (see the warning the harness prints). The harness
does **not** touch systemd.

## Where the metric lives in code

`bench/analyze.py` constants (single config surface):
- `THROTTLE_PROB_THRESHOLD = 0.65` — first predict trigger level
- `FAN_RAMPUP_RPM_THRESHOLD = 4500` — «rampup» definition
- `COOLSTEP_MOVED_FIRST_MIN_SEC = 8.0` — target lead time
- `HOT_TEMP_THRESHOLD_C = 85.0` — time-above threshold

## Cleanup

Old runs accumulate in `bench/runs/`. To prune + rebuild `index.json`:

```bash
./bench/gc.py --keep 20         # keeps newest 20, prunes rest
./bench/gc.py --dry-run         # preview without deleting
./bench/gc.py --runs-dir /alt   # alternate location
```

After each gc, `bench/runs/index.json` contains a summary per surviving run
with peak Tctl, the «coolstep moved first» metric, and verdict
(`coolstep_moved_first` / `did_not_move_first` / `not_observed` / `partial_data`).

Recommended cron: weekly `bench/gc.py --keep 50` via systemd timer (not auto-installed).

## Auto-cleanup via systemd timer

A user-level timer prunes runs weekly. Install (one-time):

```bash
cp systemd/coolstep-bench-gc.service ~/.config/systemd/user/
cp systemd/coolstep-bench-gc.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now coolstep-bench-gc.timer
```

Verify:

```bash
systemctl --user list-timers coolstep-bench-gc.timer
systemctl --user status coolstep-bench-gc.service
journalctl --user -u coolstep-bench-gc.service -n 20
```

Trigger manually:

```bash
systemctl --user start coolstep-bench-gc.service
```

The timer runs `bench/gc.py --keep 50` weekly on Sunday 03:00 local time
(with up to 10 min randomized delay).

## Dashboard integration

While the harness is running it writes `data/stress-state.json`
(removed on exit). The dashboard `/api/stress-state` route reads this file
to show the stress pill in the masthead.

## Paired baseline-vs-armed comparison

```bash
./bench/baseline.sh S1                 # default duration (per scenario)
./bench/baseline.sh S1 --duration 60   # quick check
```

Runs the same scenario twice — once dry-run, once armed — with a 30s
cooldown between. Outputs both `analyze.py` summaries plus a side-by-side
metric table for direct A/B comparison. The «moved-first delta» is the
target: armed runs should show positive delta ≥ 8s.

Outputs both run dirs at end for re-analysis.
