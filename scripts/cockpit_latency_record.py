"""30s per-second cockpit tile latency record. CSV-like output for review."""
import time, urllib.request

print("sec  latency_ms  body_age_sec")
end = time.monotonic() + 30.0
sec = 0
while time.monotonic() < end:
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen("http://127.0.0.1:18889/api/predictor-cockpit", timeout=10) as r:
            data = r.read()
        import json
        d = json.loads(data)
        age = (d.get("current") or {}).get("age_sec")
        lat = (time.monotonic() - t0) * 1000.0
        print(f"{sec:>3}  {lat:>9.1f}  {age if age is not None else 'n/a':>11}")
    except Exception as e:
        print(f"{sec:>3}  FAIL  {type(e).__name__}")
    sec += 1
    time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
