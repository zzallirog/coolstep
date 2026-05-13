"""Simulate a browser dashboard open for N seconds.
Cockpit polls at 1 Hz; sibling tiles at the cadence they actually use.
Record per-endpoint latency distribution.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import defaultdict
import urllib.request

BASE = "http://127.0.0.1:18889"

# (endpoint, period_sec) per real tile polling pattern
TILES = [
    ("/api/predictor-cockpit", 1.0),    # cockpit @ 1Hz
    ("/api/health",            10.0),   # health pill @ 10s
    ("/api/telemetry/latest",  1.0),    # live tile @ 1Hz
    ("/api/calibration",       30.0),   # calibration tile @ 30s
    ("/api/adapters",          30.0),
    ("/api/discoveries",       60.0),
    ("/api/actuator-journal",  5.0),
    ("/api/neighbours",        2.0),
    ("/api/incidents",         15.0),
    ("/api/predictor-breakdown", 2.0),
    ("/api/drift",             30.0),
    ("/api/efficiency",        30.0),
    ("/api/reliability",       60.0),
    ("/api/mode",              30.0),
    ("/api/profile",           60.0),
]

async def poll_loop(endpoint: str, period: float, until: float, latencies: list[float], fails: list[int]):
    next_at = time.monotonic()
    while time.monotonic() < until:
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(_fetch, endpoint)
            latencies.append((time.monotonic() - t0) * 1000.0)
        except Exception:
            fails[0] += 1
        next_at += period
        sleep_for = max(0.0, next_at - time.monotonic())
        await asyncio.sleep(sleep_for)


def _fetch(endpoint: str) -> None:
    with urllib.request.urlopen(BASE + endpoint, timeout=10) as r:
        r.read()


async def main(duration: float) -> int:
    until = time.monotonic() + duration
    per_endpoint: dict[str, list[float]] = defaultdict(list)
    fails: dict[str, list[int]] = defaultdict(lambda: [0])
    tasks = [
        asyncio.create_task(poll_loop(ep, period, until, per_endpoint[ep], fails[ep]))
        for ep, period in TILES
    ]
    await asyncio.gather(*tasks)

    print(f"\n{'endpoint':40} {'n':>4} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7} {'fail':>4}")
    print("-" * 84)
    rows = sorted(per_endpoint.items(), key=lambda kv: statistics.median(kv[1]) if kv[1] else 0, reverse=True)
    for ep, samples in rows:
        if not samples:
            print(f"{ep:40} no samples")
            continue
        samples.sort()
        p50 = statistics.median(samples)
        p95 = samples[int(len(samples) * 0.95)] if len(samples) >= 20 else samples[-1]
        p99 = samples[min(int(len(samples) * 0.99), len(samples)-1)] if len(samples) >= 10 else samples[-1]
        mx = samples[-1]
        print(f"{ep:40} {len(samples):>4} {p50:>7.1f} {p95:>7.1f} {p99:>7.1f} {mx:>7.1f} {fails[ep][0]:>4}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=120.0)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.duration)))
