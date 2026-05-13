# Calibration gates

> The eight checks that stand between dry-run and armed mode. None of
> them is decorative — each one closes a specific failure mode that
> caused real false-positives or false-negatives in earlier testing.

## What "armed" means

In dry-run, the predictor runs continuously, the dashboard updates
every second, the actuator router emits abstract `Action` objects, and
`readonly_log` writes them to `actuator-journal.jsonl`. Nothing reaches
hardware. This is the default mode and it ships with
`COOLSTEP_ACTUATOR_ENABLE=false`.

In armed mode, the same loop runs, but the actuator router invokes the
first matching hardware actuator's `apply()` method, and that method
calls `subprocess.run` on `asusctl`, `ryzenadj`, or the EPP sysfs node.
The fan curve actually moves. The boost limit actually drops.

The transition between the two is not a button. It is the conjunction
of five core gates plus up to three runtime-conditional gates (`cost_rss`, `cost_cpu`, `ring_warmup`), evaluated continuously. As long as all present gates are green,
the actuator can fire; if any goes red mid-session, the actuator is
disarmed automatically until it goes green again.

## The default targets vs pilot mode

Production defaults — what shipped in `coolstep/core/calibration.py`:

| Gate | Production default | "Pilot" env override (looser, for hosts that calibrate quickly) |
|---|---|---|
| `coverage_hours` | **168 h** (one full week) | `COOLSTEP_COVERAGE_HOURS=24` |
| `throttle_events` | **10 events** | `COOLSTEP_THROTTLE_EVENTS=3` |
| `peak_amplitude` | **85 °C** | `COOLSTEP_PEAK_TEMP=80` |
| `class_diversity` | **5 classes** | `COOLSTEP_WORKLOAD_CLUSTERS=3` |
| `hyprctl_consistency` | **5%** miss rate | `COOLSTEP_HYPRCTL_MISS_PCT=5` (same) |

The production defaults are stricter than the pilot defaults you'll see
on some hosts that ship with a `10-pilot-mode.conf` drop-in. If your
dashboard shows `gates: 4/5 (calibrating)` for longer than you expected,
check `systemctl --user show coolstep-collector | grep COOLSTEP_` to see
whether the pilot env overrides are in effect.

The gate body text below describes the production defaults; substitute
the pilot values wherever a number appears if the env vars say so.

## The eight gates

### 1. Coverage hours

The store must contain at least 168 hours (one week) of active samples
for this host. "Active" means `cpu_temp > 0` — sleep periods and
daemon restarts don't count. Coverage is measured as
`count(frames) × period_sec`, not span — a host that ran two hours
then was off for a day, then ran two more hours, has four hours of
coverage, not 28.

Why this matters: the KNN needs a workload mix that covers a typical
week — a CI server has weekend cycles, a laptop has Mon-Fri patterns,
a homelab gets nightly backups. Anything less than a week skews the
neighbourhood. The pilot 24-hour override is for ASUS laptops in
heavy daily use where the same workload mix repeats often enough
that 24 h is empirically adequate.

### 2. Throttle events

At least ten episodes of `cpu_temp ≥ 90 °C ≥ 3 seconds` must have
been recorded. This is the FSM threshold with hysteresis (enter at
90, exit at 85, minimum hold of 3 seconds) — the same numbers the
predictor's `was_hot_in_30s` label uses.

Why this matters: if the host never sees a throttle, the KNN has no
positive examples to vote yes on. A predictor trained only on cool
data will predict 0% throttle for everything, including the day the
user starts running compiles. Ten episodes give a meaningful positive
class, not just two or three flukes.

For hosts that genuinely never throttle (well-ventilated workstations,
under-volted laptops, server-class chassis with BMC fan control), the
gate is configurable via `COOLSTEP_THROTTLE_EVENTS` and can be lowered
to zero with the understanding that the predictor will run on the
trajectory-fallback signal alone.

### 3. Peak amplitude

The hottest single sample during the calibration window must be at
least 85 °C. Combined with gate 2, this prevents the situation where a
host accidentally crosses the 90 °C FSM threshold ten times due to
sensor noise but never actually gets meaningfully hot.

### 4. Workload class diversity

The workload classifier (CODE / GAME / RENDER / IDLE / OTHER) must
have seen at least five distinct classes during calibration.

Why this matters: a KNN that has only seen IDLE samples will not
generalize. Diversity is the cheap proxy for "the model has seen enough
of the host's actual usage to be useful." The classifier picks the
class from the focused window's resource class plus rolling load
features, so this gate is structurally tied to gate 7.

### 5. hyprctl / dbus consistency

The workload-context collector (`hyprctl` on Hyprland, `dbus_session`
on KDE/GNOME, fallback to nothing on server hosts) must have produced
a non-empty label for at least 95% of active frames. The threshold is
loose because momentary subprocess timeouts are normal.

Why this matters: workload context is the one signal coolstep cannot
get from kernel sensors. If `hyprctl clients -j` is failing 30% of the
time (often a `HYPRLAND_INSTANCE_SIGNATURE` propagation race on
session start), the model is learning on noise. The gate forces the
user to fix the underlying issue.

On server hosts where no compositor is present, this gate is
auto-satisfied — the predictor runs on hardware-only signals.

### 6. KNN warm-up

The KNN store must contain at least 5 labeled vectors. Store is
either `ChromaStore` or `HnswStore` per `COOLSTEP_KNN_BACKEND` (the
gate reads the count through the store-agnostic
`KnnStore.count_labeled()` so HNSW hosts see the same gate). On HNSW
the labeled count comes from `data/hnsw/meta.sqlite`; on chroma from
the collection. The label
(`was_hot_in_30s`) is assigned by a 30-second lookahead after each
sample: if `cpu_temp ≥ 90` happens within 30 seconds of this frame, the
frame's vector gets `label=1`, else `label=0`. The 30-second delay is
the reason the gate exists: even after 24 hours of data, the most
recent 30 seconds are still unlabeled.

The orphan-label sweep at startup promotes any vectors older than 60
seconds with `label=-1` to `label=0` to prevent indefinite warm-up
when no throttles happen.

### 7. Predictor confidence

The KNN's `confidence = coverage × agreement` must average at least 0.5
over the last 10 minutes. Coverage is the fraction of returned
neighbours that have a label; agreement is how strongly they vote in
the same direction.

Why this matters: a confident-but-wrong predictor will spam the
actuator. A low-confidence predictor that occasionally fires is fine.
The gate keeps the actuator gated to the predictor's own self-assessed
reliability.

**Per-bucket trust modes** layer on top of this gate (added in
v0.5.9). `MetaPredictor` (`coolstep/core/predictor_meta.py`) clamps
`bucket_certainty` to **0.4** when the current bucket has fewer than 5
residual samples (the "learning" regime), pulling composed confidence
*below* the base predictor's value even when the KNN itself is
self-confident. Once a bucket accumulates ≥ 5 samples, certainty
scales normally from the residual σ. Three named regimes:

| Mode | Sample count | What it means |
|---|---|---|
| `prior` | n < 2 | Bayesian prior dominates; correction is the bucket's prior mean only |
| `shrunk` | 2 ≤ n < 5 | Empirical mean shrunk toward prior by sample count; intermediate trust |
| `confident` | n ≥ 5 | Empirical mean used directly; full trust in the bucket's residual history |

The cockpit surfaces the current bucket's mode under the bucket strip
(`trust_mode` field in `/api/predictor-cockpit`). This is observable
state — the gate-7 floor on average confidence is what blocks armed
operation, but the trust mode tells the operator *why* a specific
prediction's confidence is what it is.

### 8. Re-arm gap

Once an action has fired, the actuator stays disarmed for at least 15
seconds, regardless of what the predictor says. This is a flap
protector, not a calibration check — but it lives alongside the others
because it's part of the same "is it safe to actuate" decision.

## What you see in the dashboard

The `<calibration-state-tile>` shows each gate as a row, with a
green / yellow / red marker, the current value, and the threshold.
Gates that are bottlenecks are highlighted. The masthead carries a
`calibration: 100%` pill when all eight are green.

## Overrides

Every gate has an environment variable so unusual hosts can adjust:

```
COOLSTEP_COVERAGE_HOURS=24
COOLSTEP_THROTTLE_EVENTS=3
COOLSTEP_PEAK_TEMP=80
COOLSTEP_WORKLOAD_CLUSTERS=3
COOLSTEP_COST_RSS_KB=50000     # daemon RSS budget (KB); cost_rss gate fires when present
COOLSTEP_COST_CPU_PCT=1.0      # daemon CPU budget (%); cost_cpu gate fires when present
COOLSTEP_HYPRCTL_MISS_PCT=5
COOLSTEP_THROTTLE_ENTER_C=90
COOLSTEP_THROTTLE_EXIT_C=85
COOLSTEP_MIN_ARM_LABELED_COUNT=5
COOLSTEP_MIN_ARM_CONFIDENCE=0.5
COOLSTEP_REARM_GAP_S=15
```

The values are read at module import time. Changes require a daemon
restart.

## See also

- [drift-detection.md](drift-detection.md) — the seven indicators of
  model staleness, which can disarm the actuator after it has been
  armed
- [physics-rationale.md](physics-rationale.md) — why crossing thresholds
  matters even when the kernel doesn't throttle
