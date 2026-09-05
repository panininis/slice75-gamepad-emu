"""60s W-hunt via the APP's exact VendorStream (post-fix code path).

Logs all 58 adc cells + per-key travel + OS WASD held set at ~5 Hz.
User holds W ~5s during the window (plus A/S/D briefly for reference).
Post-hoc: find cells dropping >=300 during W-held rows.
"""
import json, os, sys, time, statistics
sys.path.insert(0, "app")
from slice_capture import find_interfaces, VendorStream, OsKeys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_Whunt_app.jsonl")
open(OUT, "w").close()

ifs = find_interfaces()
vs = VendorStream(ifs["vendor"])
assert vs.open(), "vendor open failed"
osk = OsKeys()

print(f"WINDOW 60s start {time.strftime('%H:%M:%S')}", flush=True)
print(">> HOLD W ~5s on the Slice (then A, S, D ~1s each). No typing.", flush=True)
t_end = time.time() + 60.0
rows = 0
with open(OUT, "a", buffering=1) as f:
    while time.time() < t_end:
        t0 = time.time()
        with vs._lock:
            adc = dict(vs.adc)
        held = osk.poll()
        trav = {k: round(vs.travel(p), 3)
                for k, p in [("W", 35), ("A", 30), ("S", 31), ("D", 32)]}
        rec = {"t": round(time.time(), 3), "os": sorted(held),
               "adc": {str(p): v for p, v in adc.items()},
               "trav": trav, "frames": vs.frame_count}
        f.write(json.dumps(rec) + "\n")
        rows += 1
        if held and "W" in held:
            print(f"  W held @ {time.strftime('%H:%M:%S')}", flush=True)
        dt = time.time() - t0
        if dt < 0.2:
            time.sleep(0.2 - dt)
vs.close()
print(f"stop {time.strftime('%H:%M:%S')} rows={rows}", flush=True)

# ---- analysis ----
rows = [json.loads(l) for l in open(OUT) if l.strip()]
wrows = [r for r in rows if "W" in r["os"]]
rest = [r for r in rows if not r["os"]]
print(f"W-held rows: {len(wrows)}  rest rows: {len(rest)}", flush=True)

base = {}
for r in rest:
    for k, v in r["adc"].items():
        base.setdefault(k, []).append(v)
base = {k: statistics.median(v) for k, v in base.items() if v}

drops = []
for k, med in base.items():
    w = [r["adc"].get(k) for r in wrows if r["adc"].get(k) is not None]
    if not w:
        continue
    mn = min(w)
    d = med - mn
    if d >= 300:
        drops.append((int(k), round(d), int(mn), int(med)))
drops.sort(key=lambda x: -x[1])
print("== cells dropping >=300 during W-held (pos, drop, min, rest) ==")
for p, d, mn, md in drops[:12]:
    print(f"  pos{p}: drop {d}  (rest {md} -> {mn})")
if not drops:
    print("  (none)")
# reference: A/S/D during their own holds
for k, p in [("A", 30), ("S", 31), ("D", 32)]:
    held = [r for r in rows if k in r["os"]]
    if held:
        vals = [r["adc"].get(str(p)) for r in held if r["adc"].get(str(p)) is not None]
        if vals:
            print(f"  ref {k}: rest={base.get(str(p))} min={min(vals)} drop={round(base.get(str(p),0)-min(vals))} (n={len(held)})")
