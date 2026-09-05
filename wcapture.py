"""RAW full-stream W-hunt: logs EVERY report (headers + continuations)
byte-for-byte for 90 s, plus OS WASD state per report.

No parsing, no filtering — offline analysis groups frames afterwards.
ONE process, one open. Board is push-mode: also sends keep-alive sub6
requests like the app does (strictly sequential).

Usage: python -B -u wcapture.py
User: hold W ~3s, then A ~3s, then S ~3s, then D ~3s (gaps between).
Output: wasd_wcap.jsonl  (one row per report: seq, t_ms, os, hex)
"""
import json, os, sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_wcap.jsonl")
open(OUT, "w").close()
DURATION = 90.0

import ctypes
user32 = ctypes.windll.user32
VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
def os_keys():
    s = set()
    for k, v in VK.items():
        if user32.GetAsyncKeyState(v) & 0x8000:
            s.add(k)
    return "".join(sorted(s))

ifs = find_interfaces()
d = hid.device()
d.open_path(ifs["vendor"])
d.set_nonblocking(True)
time.sleep(0.3)
d.write(b"\x00" + build_sync())
time.sleep(0.5)
while d.read(65):
    pass

f = open(OUT, "a", buffering=1)
import ctypes
_k = ctypes.windll.kernel32
t_start = time.time()
seq = 0
half = 1
last_req = 0.0
print(f"WCAP live {DURATION:.0f}s -> {OUT}", flush=True)
print("10s countdown... GET READY on the Slice", flush=True)
for i in range(10, 0, -1):
    print(f"  {i}...", flush=True)
    time.sleep(1.0)
_k.Beep(880, 300)
print("NOW: W 3s ... A 3s ... S 3s ... D 3s", flush=True)
try:
    while time.time() - t_start < DURATION:
        now = time.time()
        # keep-alive request ~100 Hz (board pushes anyway; app pattern)
        if now - last_req > 0.010:
            try:
                d.write(b"\x00" + build_cmd(CMD_RM6X21, (6, half)))
                half = 2 if half == 1 else 1
            except OSError:
                pass
            last_req = now
        try:
            rep = d.read(65)
        except OSError:
            rep = None
        if rep:
            b = bytes(rep)
            if len(b) == 65 and b[0] == 0:
                b = b[1:]
            f.write(json.dumps({"s": seq, "t": round((now - t_start) * 1000, 2),
                                "os": os_keys(),
                                "x": b.hex()}) + "\n")
            seq += 1
        else:
            time.sleep(0.0003)
        # heartbeat
        if seq and seq % 3000 == 0:
            left = DURATION - (time.time() - t_start)
            print(f"  reports={seq}  {left:.0f}s left", flush=True)
except KeyboardInterrupt:
    pass
finally:
    f.close()
    print(f"DONE reports={seq} -> {OUT}", flush=True)
    d.close()
