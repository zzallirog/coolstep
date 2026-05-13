"""60s file-poll experiment: poll ml-state.json mtime at 100ms cadence.
Reports max stale gap, mean gap, and how many polls saw a freshness > 500ms.
Operator-instrumented 2026-05-13.
"""
import os, time, statistics

PATH = "/home/zzalli/coolstep/data/ml-state.json"
gaps = []
prev_mtime = os.stat(PATH).st_mtime
prev_seen = time.monotonic()
end = prev_seen + 60.0
gap_over_500ms = 0
last_advance_at = prev_seen

while time.monotonic() < end:
    time.sleep(0.1)
    cur_mtime = os.stat(PATH).st_mtime
    now = time.monotonic()
    if cur_mtime > prev_mtime:
        gap = now - last_advance_at
        gaps.append(gap)
        if gap > 0.5:
            gap_over_500ms += 1
        prev_mtime = cur_mtime
        last_advance_at = now

if gaps:
    print(f"polls: {len(gaps)}  mean_gap={statistics.mean(gaps)*1000:.0f}ms  "
          f"max_gap={max(gaps)*1000:.0f}ms  p95={sorted(gaps)[int(len(gaps)*0.95)]*1000:.0f}ms")
    print(f"writes/sec_effective: {len(gaps)/60:.1f}")
    print(f"polls where gap > 500ms: {gap_over_500ms}/{len(gaps)}")
else:
    print("no ml-state writes observed — daemon stalled?")
