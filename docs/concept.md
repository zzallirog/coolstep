# Concept

> Why this exists at all.

## The reactive controller is always late

Your laptop's thermal management is a chase. Sensors cross a threshold,
the controller reacts, the fan ramps up, the boost limiter drops. The
problem is the order of events. By the time the sensor crosses the
threshold, the silicon is already hot — and the fan is still spinning at
idle RPM because the impeller hasn't accelerated yet. A modern radiator
takes seconds to warm up; load spikes happen in milliseconds. The
controller is correct, it just operates after the fact.

The result is the noise everybody knows. Quiet for a few minutes, then a
sudden jet-engine ramp when a build kicks off, then a long tail of fan
inertia while the temperature is already dropping. The reactive loop is
not broken — it's structurally late.

## The fix is anticipation, not replacement

`thermald`, `power-profiles-daemon`, the BIOS fan curve, vendor utilities
like `asusctl` and `ryzenadj` — all of those keep doing their jobs. They
catch the hard cases. What's missing is a layer above them that notices a
spike forming before it lands and gives the slow parts of the system
(impeller, radiator, boost limiter) the head start they need to absorb
it without crossing the reactive threshold at all.

That layer is what coolstep is. It samples a small set of pre-thermal
signals — load distribution, CPU frequency rate, recent context-switch
bursts, focused-app class — every second, asks a local k-nearest-neighbour
model whether the next 30 seconds look like the historical episodes that
ended in a throttle, and if the answer is yes, it nudges the fan curve
upward in the knee band and trims the boost limit just enough that the
spike never crests. If the model is wrong, the reactive loop is still
there. We are not replacing anything; we are smoothing.

## Why each host needs its own model

A 14-inch ASUS laptop and a 1U Supermicro rack are thermally different
machines, with different airflow geometry, different paste age, different
ambient conditions. A model trained on aggregate user data would be wrong
about every individual machine in a different way. coolstep therefore
trains on this host — your apps, your workloads, your duty cycle — and
nothing else. The model file lives on disk, no telemetry is uploaded,
nothing crosses the network.

This also means there is a calibration window. For roughly 24 hours of
realistic use, coolstep records what your workload looks like and what
the thermal response is, and it stays in dry-run mode (predicts and
displays, does not actuate). After eight calibration gates clear, you
can opt in to hardware writes. Most users stay in dry-run permanently
and use coolstep as a diagnostic dashboard. Both modes are first-class.

## Two deployment targets, one engine

The same daemon runs on a quiet laptop and on a noisy rack. The signals
differ — fan RPM and EPP versus inlet temperature and BMC power
counters — and the questions a user asks differ too. A laptop user wants
to know why the fan is loud right now. A rack admin wants to know which
chassis will degrade first.

coolstep detects which side of that line you're on (compositor present?
`ipmi_si` loaded? `COOLSTEP_REDFISH_URL` in environment?) and arranges
its dashboard accordingly. The model in the middle is the same KNN; only
the inputs, outputs, and presentation change. One codebase, two faces.

## Soft-cooling, not hard

The actuators coolstep prefers are the soft ones: a fan-curve bias in
the knee band, a `balance_power` shift on `energy_performance_preference`,
a temporary lowering of fast-PPT on AMD via `ryzenadj`. All of those are
reversible within a couple of seconds, none of them break running
workloads, and all of them can be disabled with a single environment
variable.

The hard rails — kernel thermal trip, BIOS throttle, BMC-controlled
chassis fans on a server — are not coolstep's concern. They are the
backstop, and they should stay the backstop. coolstep operates in the
margin between idle and the first reactive event, and that margin is
where the noise lives.

## See also

- [physics-rationale.md](physics-rationale.md) — why thermal spikes
  matter at the silicon level (Arrhenius + Poole–Frenkel)
- [architecture.md](architecture.md) — module map and data flow
- [calibration-gates.md](calibration-gates.md) — the eight gates between
  dry-run and armed mode
- [p3-plan.md](p3-plan.md) — the two deployment targets, in design depth
