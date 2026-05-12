# Drift detection

> Seven signals that say the model knows less than it thinks it does.
> Each one disarms the actuator if it fires alone, or triggers a
> visible warning if combined with degraded confidence.

## What drift means here

The KNN predictor is trained on this host's history. The host changes.
Paste degrades. Ambient temperature changes between summer and winter.
A new workload appears that nothing in the historical neighbourhood
matches. The model keeps returning probabilities, but they're now
about a host that no longer exists.

Drift detection is the daemon's self-audit. It runs cheaply every five
minutes, writes results to `drift-history.jsonl`, and surfaces the
worst-affected indicator on the dashboard's `<drift-tile>`. When any
indicator goes red, the actuator returns to dry-run automatically;
when the indicator goes green again, the actuator can re-arm subject
to the eight calibration gates.

## The seven indicators

### 1. `ml_state_missing` / `ml_state_corrupt`

The predictor writes a snapshot to `data/ml-state.json` every 30 ticks.
The drift checker verifies the file is readable JSON with the expected
keys (`model_name`, `throttle_prob`, `confidence`, `features`,
`updated_at`). If the file is missing for more than two snapshot
periods or doesn't parse, this indicator goes red.

### 2. `snapshot_stale`

If `ml-state.json:updated_at` is more than 120 seconds old, the
predictor is wedged. Snapshot freshness is the cheapest possible
liveness check — it costs one file stat per drift run.

### 3. `embedder_cold`

The fingerprint extractor needs roughly 60 frames before it can
produce stable z-scored embeddings. Until that point, the predictor's
embeddings are unreliable. The indicator fires while
`embedder_fitted` is `False` (severity 0.4), and clears as soon as
fitting completes.

In v0.5.0 this is the full check — no temporal baseline comparison
yet. Seasonal-shift and per-weekday-baseline detection are on the
P3.5+ roadmap (`coolstep/core/drift.py`); they need an
`embedder-stats.parquet` history file that doesn't exist yet. Once
that history is in place, the same indicator name will gain a
3-σ-versus-historical check on top of the current cold-start gate.

### 4. `confidence_drop`

If the predictor's rolling `confidence` falls below 0.3 for more than
five consecutive minutes, the model has lost its grip on the current
state. This often coincides with `embedder_cold` but can fire alone
when the host has just hit a workload class never seen before.

### 5. `chroma_no_growth`

ChromaDB should accumulate new labeled vectors over time. If the
labeled vector count has been flat for over an hour while the daemon is
running (and the host is not idle), something is broken in the
labelling pipeline — usually the 30-second `was_hot_in_30s` lookahead
has stopped emitting labels for some reason.

### 6. `knn_low_confidence`

This is the per-query version of indicator 4. A single low-confidence
prediction is fine. But if 80% of the last 100 queries had
`coverage × agreement < 0.3`, the neighbourhood is empty for most
recent samples — the host's workload has moved into a region of the
embedding space the model has never seen.

### 7. `chroma_dir_bloat`

A sanity check inherited from a real incident (2026-05-04, see
the chroma-bloat incident postmortem). ChromaDB's
`link_lists.bin` can grow unboundedly under restart storms or version
mismatch. The drift checker compares `chroma_dir_bytes` against a soft
500 MB cap and a hard 5 GB cap; the soft cap warns, the hard cap
disarms the predictor and writes an incident postmortem.

## How firing translates to actuator state

The decision engine consults all seven indicators each tick. Any
indicator in `red` state disarms the actuator unconditionally for the
current tick. The actuator stays disarmed until the next drift sweep
clears the indicator.

Indicators in `yellow` state (e.g. `embedder_cold` with a 2-sigma
shift, below the 3-sigma threshold) lower the predictor's effective
confidence by 0.2 but don't disarm by themselves. This way, a host
showing mild seasonal drift gets a more conservative predictor without
losing the actuator entirely.

## When you should manually intervene

A red indicator that won't clear on its own is asking for action. The
typical interventions:

- `embedder_cold` for more than 24 hours → run
  `coolstep snapshot-archive --promote-current` to set the new
  baseline as golden.
- `confidence_drop` after a hardware change → run
  `coolstep calibration --reset` to clear the labeled vector pool and
  start a new calibration window.
- `chroma_dir_bloat` → see the incident postmortem; the fix is
  `mv data/chroma data/chroma.bak.$(date +%F)` + daemon restart.

The CLI prints the recommended command when an indicator goes red, so
you don't have to memorize this.

## See also

- [calibration-gates.md](calibration-gates.md) — what the indicators
  are protecting against in the other direction
- [stack-decisions.md](stack-decisions.md) — ADR-013 explains the KNN
  choice and the indicators that audit it
