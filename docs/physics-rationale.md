# Physics rationale

> Why thermal spikes matter at the silicon level — and why smoothing them
> is worth doing.

## The exponential cost of heat

Silicon does not fail gracefully. The dominant failure modes — electromigration
in copper interconnects, dielectric breakdown in gate oxides, NBTI shift
in PMOS thresholds — all follow Arrhenius kinetics:

```
rate(T) = A · exp(−Eₐ / kT)
```

In practice, this means failure rate roughly doubles for every 10 °C
rise in junction temperature once you're past about 70 °C. A CPU that
spends a long Sunday afternoon at 88 °C is aging measurably faster than
one that crests at 82 °C for the same workload. The integral over time
matters, but the peaks dominate because of the exponential.

This is why "it didn't throttle" is a weak claim. Throttling is the
last-resort safety net at 95 °C or so. Damage accumulates well below
that. The interesting region is 78–88 °C — high enough to age silicon
visibly, low enough that the thermal controller is still asleep.

## Spikes are worse than averages

Two workloads that integrate to the same total joules can age the chip
differently. A long, flat 75 °C compile is gentle. A pattern of short
90 °C spikes that average out to 75 °C is harsh, because Arrhenius is
exponential in `T`. The instantaneous rate during each spike is much
higher than the rate at the average.

Worse, spikes excite the second failure mechanism: Poole–Frenkel emission
in dielectric defects. A rising temperature transient lowers the
activation barrier for charge tunneling through gate oxide. Each spike
adds a tiny amount of trapped charge. The accumulated damage is
non-linear in the number of cycles, not the total time hot.

The conclusion: smoothing the peaks matters more than lowering the
average. A coolstep that keeps you at 82 °C steady is gentler than a
factory curve that ping-pongs between 65 and 92 °C, even if the average
is identical.

## Why the reactive controller is structurally late

A fan has impeller inertia. From idle RPM to a useful fraction of full
speed takes 1–3 seconds. The radiator takes another second or two to
move the heat from the IHS to the airflow. Together that's 2–5 seconds
of thermal lag once the controller has decided to act.

The CPU workload, in contrast, can transition in well under a hundred
milliseconds. A `make -j16` launching, a game level loading, a `zen`
build kicking off — all of these spike load to saturation in a single
scheduler tick. The silicon heats faster than the fan can spin up.

So the reactive controller, even if it had infinitely fast sensors and a
perfect curve, would still be late by the impeller and radiator time
constants. Closing that gap requires noticing the workload before the
temperature has moved — which is exactly what scheduler, frequency and
cache-miss signals enable. By the time the temperature rises, the fan is
already at the target RPM and the boost limit is already trimmed.

## Why 10 ms eBPF beats 1 Hz `/proc/stat`

A scheduler `sched_switch` burst happens in microseconds. The eBPF
tracepoint sees it immediately. A `/proc/stat` poll at 1 Hz sees it as
an averaged increment in the next sample, smoothed across the rest of
the second. The difference matters when the workload-classifier is
trying to distinguish a brief Chrome JIT compile from the start of a
sustained build.

This is why coolstep wants `bpftrace` and `perf stat -I 1000` available
when the host kernel allows it. Each adds ~50 ms of headroom to the
prediction. Combined with the ~3 s impeller lag, that's the difference
between "fan started spinning up just in time" and "fan was at target
by the time the load actually landed."

## Why we operate in the soft margin

Hard rails — kernel thermal trip, BIOS throttle, RAPL hard cap — are
protective. They exist to prevent damage in the worst case. They should
not fire often. Every hard-rail event is a small irreversible age
penalty (electromigration prefers high current, which the throttle is
trying to limit) plus a user-visible perf cliff.

The soft margin is the band between idle and the first hard-rail event.
That is where coolstep operates. A 5% fan-curve bias in the 78–85 °C
knee, a `balance_power` shift on EPP for 20 seconds, a temporary
fast-PPT reduction — none of these break anything, all of them are
reversible in under a second, and combined they keep the silicon out of
the high-Arrhenius zone.

## What "soft" means in practice

A coolstep actuator commits to three properties:

1. **Reversible** within a couple of seconds. The fan curve goes back to
   the user's baseline on the next tick when the prediction stops firing.
2. **Bounded.** Bias intensity caps at 20% in the knee band, never
   exceeds the user's maximum-pwm, and never crosses below the
   fan-stop floor (which exists for liquid-metal and premium-paste
   machines that benefit from passive idle).
3. **Observable.** Every apply and revert event lands in
   `actuator-journal.jsonl` and on the dashboard `<actuator-history>`
   tile. Nothing is silent.

If any of those fail, the actuator is by definition not soft, and it
does not ship.

## See also

- [efficiency-curve.md](efficiency-curve.md) — the `work_per_degree`
  proxy and how the sweet spot is detected
- [stack-decisions.md](stack-decisions.md) — ADR-010 explains why we
  wrap existing tools instead of writing to hwmon directly
- [calibration-gates.md](calibration-gates.md) — what "calibrated enough
  to arm actuators" actually means in numbers
