"""How quickly does a mixed list become useful?

Runs a list that is mostly already-resolved with some brand-new titles mixed in
at the front, against a server on localhost:8080, and prints how the answered
count climbs. The point is that the new titles being first in the file no longer
means waiting for them.
"""

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def post(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path))


KNOWN = [{"title": f"Familiar Title {i}", "author": f"Writer {i}"} for i in range(400)]
FRESH = [{"title": f"Brand New Title {i}", "author": f"Newcomer {i}"} for i in range(100)]
SCOPES = ["westmount", "queenslibrary", "aubora"]

print("warm-up run so the ids get learned…")
job = post("/api/jobs", {"books": KNOWN, "scopes": SCOPES})
while get(f"/api/jobs/{job['job_id']}")["state"] == "running":
    time.sleep(0.5)

# Drop the availability cache but keep the learned ids — the state a saved list
# is in the next morning.
post("/api/cache/clear")

mixed = FRESH[:50] + KNOWN + FRESH[50:]   # new titles deliberately at the front
job = post("/api/jobs", {"books": mixed, "scopes": SCOPES})
jid = job["job_id"]
print(f"\n{len(mixed)} books ({len(KNOWN)} known, {len(FRESH)} new); "
      f"server estimate {job['estimated_seconds']}s\n")

start = time.time()
marks = []
while True:
    status = get(f"/api/jobs/{jid}")
    marks.append((time.time() - start, status["done"]))
    if status["state"] != "running":
        break
    time.sleep(0.4)

for at, done in marks[::3]:
    bar = "#" * int(done / len(mixed) * 40)
    print(f"  t+{at:5.1f}s  {done:4d}/{len(mixed)}  {bar}")
print(f"\nfinished in {marks[-1][0]:.1f}s")

rate = get("/api/health").get("rate", {})
if "started_at" in rate:   # the mock provider has no limiter to report on
    print(f"rate ended at {rate['rate_per_minute']}/min (started {rate['started_at']}, "
          f"{rate['speed_ups']} speed-ups, {rate['throttled']} throttles)")
