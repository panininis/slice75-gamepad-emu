"""Persistent RAW logger, web-driver request pattern.

Sends the vendor driver's sequence (sub 2/2/6/6/11 ...) in a cycle so ALL
bank streams (sub-6 A/B + sub-11 R1/R2) flow. Logs EVERY 0x92-class frame
raw (head + first 34 bytes + 29 cells from both offsets) plus OS WASD.
Runs until killed. Press W (and A/S/D) on the Slice whenever, then message.

Output: wasd_rawall2.jsonl
"""
import json, os, sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_rawall2.jsonl")
open(OUT, "w").close()

# web-driver sequence (from driver_main.js travel test)
SEQ = [(2, 1), (2, 2), (6, 1), (6, 2), (11, 1), (11, 2)]

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
time.sleep(0.4)
for _ in range(300):
    if not d.read(65):
        time.sleep(0.001)

print(f"raw-all2 logger live -> {OUT} (Ctrl-C to stop)", flush=True)
f = open(OUT, "a", buffering=1)
tick = 0
n = 0
last_hb = 0.0
try:
    while True:
        now = time.time()
        if now - last_hb >= 2.0:
            try:
                d.write(b"\x00" + build_sync())
            except OSError:
                pass
            last_hb = now
        sub, half = SEQ[tick % len(SEQ)]
        tick += 1
        try:
            d.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
        except OSError:
            pass
        for _ in range(20):
            try:
                rep = d.read(65)
            except OSError:
                break
            if rep:
                b = bytes(rep)
                if len(b) == 65 and b[0] == 0:
                    b = b[1:]
                if b and len(b) >= 12 and b[2] == 0x92:
                    # log head + cells at offset 6 (standard) and 2 (sub11)
                    c6 = [int.from_bytes(b[6+2*i:8+2*i], "little")
                          for i in range(min(29, (len(b)-6)//2))]
                    c2 = [int.from_bytes(b[2+2*i:4+2*i], "little")
                          for i in range(min(29, (len(b)-2)//2))]
                    f.write(json.dumps({"t": round(now, 4), "os": os_keys(),
                                        "h": b[0], "b1": b[1], "sub": b[5],
                                        "c6": c6, "c2": c2}) + "\n")
                    n += 1
            else:
                time.sleep(0.0003)
        if time.time() - now < 0.006:
            time.sleep(0.006 - (time.time() - now))
        if n and n % 3000 == 0:
            print(f"rows={n} {time.strftime('%H:%M:%S')}", flush=True)
except KeyboardInterrupt:
    pass
finally:
    f.close()
    print("stopped, rows:", n, flush=True)
