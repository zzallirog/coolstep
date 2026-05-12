# Curve ownership — who manages what, and where coolstep ends

> When your fan ramps up, multiple actors had a hand in the decision —
> BIOS, vendor tool, the user's chosen profile, and (sometimes) coolstep.
> This document maps the chain of authority and the exact moment coolstep
> stops and someone else takes over.

## TL;DR

```
┌──────────────────────────────────────────────────────────────────┐
│  HARD floor — kernel + BIOS thermal trip                         │
│  Owner: BIOS firmware + Linux kernel                             │
│  coolstep never touches this. Final safety net at ~95 °C.        │
└──────────────────────────────────────────────────────────────────┘
              ▲ never crossed in healthy operation
              │
┌──────────────────────────────────────────────────────────────────┐
│  Vendor curve — actual PWM-to-temperature mapping                │
│  Owner: asusctl / ec_sys / nbfc / nct6687-dkms / BMC             │
│  coolstep BIASES anchors in the knee band, never replaces curve. │
└──────────────────────────────────────────────────────────────────┘
              ▲ this is where coolstep operates
              │
┌──────────────────────────────────────────────────────────────────┐
│  User profile — Quiet / Balanced / Performance preset            │
│  Owner: you (asusctl --profile Quiet, or BIOS, or DE app)        │
│  coolstep RESPECTS your choice — never auto-switches profiles.   │
└──────────────────────────────────────────────────────────────────┘
              ▲
              │
┌──────────────────────────────────────────────────────────────────┐
│  coolstep predictive bias — a +5% nudge in the knee band         │
│  Owner: coolstep, only when actuator armed + prediction fires    │
│  Reversible within seconds. Audited in actuator-journal.jsonl.   │
└──────────────────────────────────────────────────────────────────┘
```

## What "curve" means in three different mouths

Three layers all called "the curve" by different documentation. They
are not the same object.

**The vendor curve** is what `asusctl fan-curve` reads and writes. On
a TUF A15 it's eight anchor points like
`60c:0%,65c:10%,70c:25%,73c:40%,76c:55%,80c:75%,85c:90%,90c:100%`.
Below 60 °C the fan stops; at 90 °C it's at 100% PWM. This is the
actual hardware curve the EC follows when the fan controller decides
how fast to spin.

**The user profile** is the `Quiet / Balanced / Performance` cycle
that ships with most laptops. Each profile is *a different vendor
curve plus different power limits*. Switching from Balanced to Quiet
on an ASUS laptop simultaneously: lowers the fan-curve anchors,
lowers fast-PPT, lowers slow-PPT, possibly drops the EPP target. The
profile is one switch, three or four underlying changes.

**The coolstep bias** is a temporary modification on top of the
current vendor curve. It's not a new curve — it's a transformation
applied to whatever curve the user currently has. When the bias
expires, the curve goes back to exactly the bytes it had before
coolstep touched it.

## How coolstep discovers the user's curve

Before any modification, the daemon reads the user's curve from
asusctl:

```
asusctl fan-curve --mod-profile $PROFILE --fan cpu  # read
```

The anchors come back as `60c:0%,65c:10%,...`. coolstep stores this
in `data/asusctl_fan_curve_baseline.json` as the **baseline**.

The baseline is re-snapshotted every five minutes while idle, so if
you manually change your fan curve via `asusctl` while coolstep is
running, the baseline updates to match. The next bias is computed
relative to your *new* curve, not the curve from when the daemon
started.

If the daemon crashes hard and the cleanup script runs, it reads
this baseline and re-applies it — that's what prevents the
SIGKILL/OOM case from leaving your curve in a biased state.

## What "+5% on the knee" actually does

Take the user's current curve, eight anchor points:

```
60°C : 0%
65°C : 10%
70°C : 25%
73°C : 40%   ◄── knee band starts here
76°C : 55%   ◄──
80°C : 75%   ◄── knee band ends here
85°C : 90%
90°C : 100%
```

The "knee band" is configurable but defaults to 70–85 °C — the
temperatures at which a small bias makes the biggest perceptual
difference (above this, fans are already loud; below this, fans don't
contribute meaningfully). Inside the band each anchor gets a weighted
add:

```
weight(t) = trapezoid centered on 75–80 °C
  full inside the band, linear ramp at 70/85, zero outside
intensity_pct = 5 to 20, scaled by throttle_prob
final = clamp(anchor + weight × intensity_pct, anchor, 100)
```

With a 5% intensity at maximum weight, the curve becomes:

```
60°C : 0%   (unchanged — below knee)
65°C : 10%  (unchanged)
70°C : 25%  (unchanged — edge of band, weight = 0)
73°C : 43%  ◄── +3, weight ramping up
76°C : 60%  ◄── +5, full weight
80°C : 80%  ◄── +5, full weight
85°C : 90%  (unchanged — edge of band, weight = 0)
90°C : 100% (unchanged)
```

The fan spins ~5% faster in the most-likely-to-throttle band. The
fan-stop point (60 °C : 0%) is preserved — if you're idle, coolstep
doesn't make the fan audible. The reactive ceiling (90 °C : 100%) is
preserved — coolstep doesn't open the throttle below where thermald
would intervene anyway.

When `throttle_prob` drops below the threshold, coolstep writes the
baseline curve back. The whole episode is one `asusctl fan-curve
--data ...` to apply, one to revert.

## Sweet spot — defined precisely

The **sweet spot** is the temperature bucket where your chip delivers
the highest `work_per_degree`. It's computed per-host from real
samples — coolstep doesn't guess.

The formula:

```
work_per_degree = (load × freq_mhz / 1000) × 100 / (T_chip − T_ambient)
```

Group samples into 1 °C buckets, compute the mean `work_per_degree`
in each bucket. The bucket with the highest mean is the sweet spot.
For a 7940HS with liquid metal and a quietify-mid curve, the sweet
spot lands around 82–85 °C — that's where this specific chip in this
specific cooling system delivers the most work for the least heat.

A different chip in a different cooling environment has a different
sweet spot. A Xeon in a 1U with aggressive BMC fans hits its sweet
spot closer to 70 °C. The per-host curve is the point.

## Knee — defined precisely

The **knee** is the first temperature bucket past the sweet spot
where mean `work_per_degree` drops below 70% of the peak. It's the
inflection point where the chip starts being clearly less efficient —
boost rolling off, leakage rising, scheduling fighting back.

The knee is where the actuator wants to act. Below it, the chip is
still in the efficient zone — no need to spin fans harder. Above it,
the chip is throwing thermal budget away.

When coolstep biases the curve, it raises anchors just below the
knee so the fan ramps before crossing it. The goal is not to keep
the chip cold — the goal is to keep it from crossing the knee.

## How predictive differs from reactive

A reactive controller waits for the temperature to cross a threshold,
then ramps. coolstep watches earlier signals — cache-miss rate
climbing, `sched_switch` bursts, focused-app class shifting to
CODE/RENDER/GAME — and asks the KNN whether the next 30 seconds look
like historical episodes that ended in a knee crossing. If the answer
is yes, the bias goes in *before* the temperature has moved.

The model never sees the user's curve. It only sees the 26-dim
embedding of telemetry. The bias is independent of what curve you've
chosen — coolstep adapts to your curve, not the other way around.

This is why the dashboard never says "your curve is wrong" or "switch
to Performance". It says "this workload pattern usually ends in a
spike — biased the knee by 5% for 30 seconds." Your profile choice
stays your choice.

## Per-vendor coverage map

Where coolstep's reach actually extends, by hardware.

### CPU vendors

| Vendor | Read (telemetry) | Write (actuator) | How |
|---|---|---|---|
| **AMD** Ryzen / EPYC | temps via `k10temp`, freq via `cpufreq`, power via `rapl_energy` (if `CAP_SYS_ADMIN`) | STAPM / fast-PPT / slow-PPT | `ryzenadj` wrapper, sudoers NOPASSWD |
| **Intel** Core / Xeon | temps via `coretemp`, power via `rapl_energy`, EPP via sysfs | EPP shift, `intel_pstate` boost cap | sysfs write, `CAP_SYS_ADMIN` drop-in |
| **ARM** Cortex / Apple | thermal zones via `arm_thermal` | nothing yet | observation only |

### GPU vendors

| Vendor | Read | Write |
|---|---|---|
| **AMD** Radeon | temps + pp_dpm + power via `amdgpu` collector | nothing — vendor tools manage |
| **NVIDIA** | temps + power + sclk via `nvidia_nvml` | nothing — `nvidia-smi -pl` is opt-in only |
| **Intel** UHD/Iris Xe/Arc | temps + freq + throttle via `intel_i915` | nothing — vendor tools manage |

### Fan controllers

| Vendor | Read fan RPM | Write fan curve |
|---|---|---|
| **ASUS** ROG/TUF | `asus_custom_fan_curve` hwmon | `asusctl_fan_curve_bias` actuator (polkit) |
| **ASUS** modern boards | `asus-ec-sensors` (in-tree) | not in v0.5.0 — vendor tool wrap planned |
| **MSI / Gigabyte / ASRock** | `nct6687-dkms` or similar | not in v0.5.0 |
| **Dell / Lenovo** | `dell-smm` or `thinkpad_acpi` | not in v0.5.0 — `nbfc-linux` recommended |
| **Generic SuperIO** | `nct6xxx`, `it87xx`, `f71xxx`, `w83xxx` | not in v0.5.0 |
| **Server BMC** | `redfish` / `ipmi` | never — BMC owns chassis fans |

### What "vendor manages curve" means in cells where coolstep doesn't write

The pattern is identical across the not-supported cells: the vendor
ships a CLI tool that owns the curve, you set it once via that tool,
and coolstep contents itself with reading sensors and showing you the
dashboard. The `notify_send` actuator is the fallback — coolstep
warns you when it would have biased the curve, with the exact
suggestion: "Predicted thermal spike. If you ran `nbfc-set-target
50`, it would absorb this." You then decide if you want to enable
hardware writes for that specific tool (community pointers in the
install plan list the supported wrappers per chip).

## Where coolstep's authority *ends*

coolstep does not:

- **Override the BIOS thermal trip.** The 95 °C hard cap stays the
  hard cap. coolstep operates below it; it never raises a ceiling.
- **Auto-switch user profiles.** If you're on Quiet, coolstep stays
  in Quiet's curve. It biases the curve, it does not change to
  Balanced when load rises. You make that call.
- **Manage liquid AIO pumps, custom water loops, or external
  cooling.** Those have their own controllers; coolstep doesn't see
  them and wouldn't know how to drive them.
- **Replace `thermald` / `power-profiles-daemon` / TLP / auto-cpufreq.**
  Those manage power profiles at a higher level. coolstep biases
  curves at a lower level. They coexist.
- **Decide which workload to throttle.** Process-level scheduling is
  not coolstep's domain. We bias the curve so processes don't *have*
  to be throttled.
- **Modify cooling on a host whose hardware-write path isn't
  available.** Without `asusctl` / `ryzenadj` / equivalent, coolstep
  stays in monitoring mode forever. The `notify_send` actuator may
  warn but never writes.

## How to read your own setup

```bash
coolstep compat                  # what we discovered on this host
coolstep adapters                # which collectors and actuators activated
coolstep efficiency --since 7d   # current sweet spot and knee for this host
asusctl fan-curve                # current curve (read your baseline)
cat ~/coolstep/data/asusctl_fan_curve_baseline.json   # what coolstep thinks your baseline is
journalctl --user -u coolstep-collector -n 50        # recent actuator events
```

The first command says what kind of host this is. The third gives
you the per-host sweet spot in numbers, not generalities. The fifth
shows what coolstep would revert to if you stopped the daemon right
now — that's the contract.

## See also

- [efficiency-curve.md](efficiency-curve.md) — the formal definition
  of `work_per_degree`, sweet spot and knee
- [physics-rationale.md](physics-rationale.md) — why peaks are worse
  than averages, why we operate in the soft margin
- [privileges.md](privileges.md) — what each actuator actually needs
  from the OS in order to write
- [calibration-gates.md](calibration-gates.md) — the eight gates
  between dry-run and armed mode
- [stack-decisions.md](stack-decisions.md) — ADR-010 records the
  decision to wrap vendor tools, not write to hwmon directly
