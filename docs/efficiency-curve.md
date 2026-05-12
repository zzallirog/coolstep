# Thermal efficiency curve

> A single number that captures how hard your CPU is willing to work for
> each degree of temperature it gives up. The curve has a sweet spot; the
> curve has a knee. Knowing where both are is what makes a fan-curve bias
> intelligent.

## The proxy

Modern AMD desktop chips don't expose CPU package power on Linux —
`k10temp` doesn't report it, and `amd_energy` is only available on a
subset of platforms. RAPL via powercap works on Intel and on some AMD
kernels, but it's root-only after CVE-2020-8694. We needed a measure of
thermal efficiency that works on every host, including the ones where
true package power is unreadable.

The proxy is `work_per_degree`:

```
work_per_degree = (load × freq_mhz / 1000) × 100 / (T_chip − T_ambient)
```

`(load × freq_mhz / 1000)` is a rough scalar for "how much actual work
the core is doing right now" — load fraction times clock rate in GHz.
The denominator is the thermal driving force: how much above ambient the
chip is. Higher numerator means more work; lower denominator means less
heat per unit work. So a high `work_per_degree` is "this chip is being
efficient: lots of work, not much heat per degree."

It is not a physical efficiency in the thermodynamic sense. It's a
*proxy*, and it's noisy. But it has two properties that matter:

- It needs no privileged reads, no out-of-tree drivers, no extra deps.
- It is monotonic in the right direction on every host we've tested.

## The sweet spot

If you bucket samples by chip temperature and compute the mean
`work_per_degree` in each bucket, you get a curve that rises until some
peak temperature and then falls. The peak is the **sweet spot** —
the temperature at which this chip happens to deliver the most work per
degree of heat.

On a 7940HS in liquid metal at room temperature, the sweet spot lands
around 82–85 °C. On a Xeon in a 1U chassis with aggressive BMC fans, it
sits closer to 70 °C. Every chip is different, and every cooling system
shifts the curve. The point of computing it per-host is precisely that
we don't have to guess.

The sweet spot is the temperature coolstep should aim for if it had any
say. It's where the chip wants to live.

## The knee

To the right of the sweet spot, efficiency drops. There's a temperature
above which the chip is doing meaningfully *less* work per degree —
boost is rolling off, leakage is rising, scheduling is fighting back.
That inflection point is the **knee**, defined as the first temperature
bucket past the sweet spot where mean efficiency drops below 70% of the
peak.

The knee is where the actuator wants to act. Letting the chip cross the
knee is throwing thermal budget away — it's not getting commensurate
work back. The fan-curve bias targets the knee band specifically:
anchors below the knee are untouched, anchors above get raised by an
intensity-scaled amount, ramping linearly to zero at the boundary.

## Why not just package power

If we had reliable `package_power_w`, the proxy could be
`work / power_w` and we'd have actual thermodynamic efficiency. We
don't on most hosts. The RAPL path is the future direction; ADR-014
documents the choice to ship the proxy now and upgrade later when
`rapl_energy` is universally readable.

The good news: the proxy and the real thing agree on which bucket is
the sweet spot and where the knee is. They disagree on the absolute
efficiency value, but both yield the same actuator decisions. We
verified this on hosts where both signals were available.

## How it's exposed

The dashboard's `<efficiency-tile>` is a Canvas line chart with markers
for the sweet spot and knee, and a live cursor showing the current
temperature bucket's mean efficiency. The curve is computed over a
rolling 7-day window by default, fitted once an hour.

The CLI version:

```bash
coolstep efficiency --since 7d
```

returns a table of (temperature_bucket, sample_count, mean_eff,
p50_eff, p95_eff). The REST endpoint `/api/efficiency` returns the
same data as JSON.

## When the curve goes flat

A flat curve means one of two things. Either the host hasn't been hot
enough to have any data above the sweet spot (so the knee is unknown
and the actuator stays in dry-run), or the cooling has degraded to the
point where the chip thermally throttles before it can show the
inefficiency. The first case clears itself with more data; the second
is one of the drift-detection signals.

## See also

- [drift-detection.md](drift-detection.md) — when the curve shifts
  meaningfully, the predictor is told to re-calibrate
- [stack-decisions.md](stack-decisions.md) — ADR-014 records the
  decision to ship the proxy instead of waiting for universal RAPL
- [physics-rationale.md](physics-rationale.md) — why the knee exists
  in the first place
