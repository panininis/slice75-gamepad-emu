"""Persistent all-sensor WASD logger (runs until killed, no GUI).

Continuously logs EVERY one of the 58 Hall sensors + the OS-held WASD set to
wasd_daemon.jsonl (~18 Hz) so that whenever the user presses keys (they press
right before messaging), the raw analog + OS ground truth is captured on disk.
No window, no timing, no coordination. I read the file after the user presses.

Robust for long runs: a single bad read never kills it; heartbeat every 60 s.
"""
from __future__ import annotations
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import (find_interfaces, VendorStream, OsKeys,  # noqa: E402
                           ADC_CELLS_PER_HALF)

HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_daemon.jsonl")
N_POS = ADC_CELLS_PER_HALF * 2

# fresh log
open(JL, "w").close()
ifs = find_interfaces()
if "vendor" not in ifs:
    print("vendor not found"); sys.exit(2)
ven = VendorStream(ifs["vendor"])
print("vendor open:", ven.open(), flush=True)
osk = OsKeys()
print(f"OsKeys.available={osk.available} fw={ven.fw_info!r}", flush=True)
print("=== DAEMON LOGGING all 58 sensors + OS WASD -> " + JL, flush=True)

last_hb = 0.0
n = 0
with open(JL, "a") as jl:
    while True:
        try:
            with ven._lock:
                snap = dict(ven.adc)
            held = osk.poll()
            rec = {"t": round(time.time(), 3),
                   "os": sorted(held),
                   "raw": {p: snap.get(p) for p in range(N_POS)}}
            jl.write(json.dumps(rec) + "\n")
            n += 1
        except Exception as e:
            print("tick err:", repr(e), flush=True)
        now = time.time()
        if now - last_hb >= 60.0:
            last_hb = now
            print(f"  ... {n} rows, frames={ven.frame_count}, "
                  f"last_os={rec['os']}", flush=True)
        time.sleep(0.055)
