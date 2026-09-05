"""Find W's TRUE sensor position (and re-confirm A/S/D) by OS-correlated
all-sensor scan.

For ~90 s it polls the vendor ADC (both halves) at ~150 Hz and, at ~10 Hz,
records for EVERY sensor position its raw value plus the OS-held WASD set.
At the end it computes, for each key K, the set of positions whose raw value
drops >600 counts (a real Hall press) during windows the OS confirms K is
held, and reports the position with the largest median drop.  This pinpoints
the true sensor for each key with no guessing.

Writes wasd_sensor_scan.json.
"""
from __future__ import annotations
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import (find_interfaces, VendorStream, OsKeys,  # noqa: E402
                           ADC_CELLS_PER_HALF)
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
DUR = 90.0
ifs = find_interfaces()
print(f"interfaces: {list(ifs)}", flush=True)
if "vendor" not in ifs:
    print("vendor not found"); sys.exit(2)
ven = VendorStream(ifs["vendor"])
print("vendor open:", ven.open(), flush=True)
osk = OsKeys()
time.sleep(1.5)
print(f"fw={ven.fw_info!r} frames={ven.frame_count}", flush=True)

N_POS = ADC_CELLS_PER_HALF * 2   # 0..57
all_pos = list(range(N_POS))
SNAP: list[tuple[dict, frozenset]] = []
def raw_snap():
    with ven._lock:
        return dict(ven.adc)

print("\n=== 90s: press W/A/S/D, hold each 2-3 s, a few times ===", flush=True)
# samples[pos] = list of (raw, heldset) ; baseline tracked per pos
t_end = time.time() + DUR
last = 0.0
prev_os = frozenset()
while time.time() < t_end:
    snap = raw_snap()                    # {pos: raw}
    held = frozenset(osk.poll())
    if held != prev_os:
        print(f"  OS {'HELD ' + str(sorted(held)) if held else 'idle'}", flush=True)
        prev_os = held
    now = time.time()
    if now - last >= 0.5:
        last = now
        print(f"  ... {int(t_end-now)}s left  frames={ven.frame_count}", flush=True)
    # stash
    SNAP.append((snap, held))
    time.sleep(0.006)

ven.close()
print(f"\ncaptured {len(SNAP)} snapshots", flush=True)

# baseline = median of all samples where NO key held (rest)
rest = [s[0] for s in SNAP if not s[1]]
base = {}
for p in all_pos:
    vals = [r[p] for r in rest if p in r and r[p] is not None]
    if vals:
        base[p] = statistics.median(vals)

# per key: for each pos, median drop during that key's held windows
def key_result(k):
    held_snap = [s[0] for s in SNAP if k in s[1]]
    if not held_snap:
        return None
    rows = []
    for p in all_pos:
        if p not in base:
            continue
        drops = [base[p] - r[p] for r in held_snap if p in r and r[p] is not None]
        if not drops:
            continue
        med = statistics.median(drops)
        rows.append((p, round(med, 1)))
    rows.sort(key=lambda x: -x[1])
    return rows

print("\n=== per-key: top positions by median drop (baseline-relative) ===", flush=True)
out = {}
for k in "WASD":
    rr = key_result(k)
    if not rr:
        print(f"  {k}: no OS-held samples")
        out[k] = None
        continue
    top = rr[0]
    print(f"  {k}: top {len(rr)} movers; #1 pos={top[0]} (half{top[0]//ADC_CELLS_PER_HALF+1}, "
          f"idx{top[0]%ADC_CELLS_PER_HALF}) median_drop={top[1]}")
    print(f"      next: {rr[1:5]}")
    out[k] = rr[:5]
with open(os.path.join(HERE, "wasd_sensor_scan.json"), "w") as f:
    json.dump({"baseline": {str(p): round(v, 1) for p, v in base.items()},
               "top5": out}, f, indent=2)
print("saved wasd_sensor_scan.json", flush=True)
