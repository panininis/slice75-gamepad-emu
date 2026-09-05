"""30-second RAW capture: dump EVERY RM6X21 response frame (raw bytes) +
OS WASD state. No filtering at all — post-hoc analysis classifies banks
and looks for ANY cell moving (up or down), including idle-zero cells.
"""
import json, sys, time, os
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_raw30.jsonl")

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
d = hid.device(); d.open_path(ifs["vendor"]); time.sleep(0.3)

# 5 s settle
t0 = time.time()
while time.time() - t0 < 5.0:
    d.write(build_cmd(CMD_RM6X21, (6, 1)))
    for _ in range(30):
        if d.read(65): break
        time.sleep(0.0004)
    time.sleep(0.0004)

start = time.time()
print("WINDOW 30s start %s — HOLD W a few seconds" % time.strftime("%H:%M:%S"), flush=True)
n = 0
with open(OUT, "w") as f:
    while time.time() - start < 30.0:
        t = time.time()
        d.write(build_cmd(CMD_RM6X21, (6, 1)))
        # drain ALL pending frames this tick (board may stream several)
        for _ in range(60):
            b = d.read(65)
            if b and len(b) >= 12:
                f.write(json.dumps({"t": round(t, 4), "os": os_keys(), "f": list(b[:65])}) + "\n")
                n += 1
            else:
                time.sleep(0.0004)
        time.sleep(0.0004)
print("stop %s frames=%d" % (time.strftime("%H:%M:%S"), n), flush=True)
d.close()
print("DONE", flush=True)
