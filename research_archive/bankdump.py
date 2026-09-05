"""Dump the exact cell patterns of both banks (idle) so the classifier can
be built on the real invariant, not a guess.
"""
import sys, time, collections
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device()
d.open_path(ifs["vendor"])
d.set_nonblocking(True)
time.sleep(0.3)
d.write(b"\x00" + build_sync())
time.sleep(0.4)

patA = {}
patB = {}
seen = set()
t0 = time.time()
while time.time() - t0 < 12:
    d.write(b"\x00" + build_cmd(CMD_RM6X21, (6, 1)))
    for _ in range(30):
        try:
            rep = d.read(65)
        except OSError:
            break
        if not rep:
            time.sleep(0.0004)
            continue
        b = bytes(rep)
        if len(b) == 65 and b[0] == 0:
            b = b[1:]
        if b and len(b) >= 60 and b[2] == 0x92:
            cells = [int.from_bytes(b[6+2*i:8+2*i], "little") for i in range(29)]
            if not any(c == 0 for c in cells):
                sig = ("LIVE",)
            sig = tuple(0 if c == 0 else 1 for c in cells)
            key = sig
            if key not in seen:
                seen.add(key)
                print("distinct frame pattern found:")
                print("  cells:", cells)
                dead = [i for i, c in enumerate(cells) if c == 0]
                print("  dead idx:", dead)
                print()
            time.sleep(0.0004)

print(f"total distinct patterns: {len(seen)}")
d.close()
