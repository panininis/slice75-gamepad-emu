"""COMPLETE sub-sweep (0..15): for each sub, stream 200 frames and
classify every distinct bank signature (by dead-cell pattern + first
cells). Reveals the FULL set of sensor banks the board exposes and how
many live cells each has. Run while the board is healthy.

Goal: if total live cells >> 76, W's Hall cell is in a bank we haven't
been sampling (untested subs 4,7,8,9,10,12,13).
"""
import sys, time, collections
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); d.set_nonblocking(True)
time.sleep(0.3)
d.write(b"\x00" + build_sync()); time.sleep(0.4)
for _ in range(300):
    if not d.read(65):
        time.sleep(0.002)

def parse_cells(b):
    # data offset: sub6 banks start at byte 6; sub11 banks at byte 2 or 3.
    for off in (6, 2, 3):
        cells = [int.from_bytes(b[j:j+2], "little") for j in range(off, min(len(b)-1, off+58), 2)]
        live = [c for c in cells if c > 800]
        if len(live) >= 4:
            return off, cells
    return None, None

summary = {}
for sub in range(0, 16):
    sigs = collections.Counter()
    sample = {}
    for _ in range(200):
        d.write(build_cmd(CMD_RM6X21, (sub, 1)))
        got = False
        deadline = time.time() + 0.010
        while time.time() < deadline and not got:
            rep = d.read(65)
            if rep:
                b = bytes(rep)
                if len(b) == 65 and b[0] == 0:
                    b = b[1:]
                if len(b) >= 12 and b[2] == 0x92:
                    off, cells = parse_cells(b)
                    if cells:
                        live = tuple(c for c in cells if c > 800)
                        key = (b[0], off, len(live))
                        sigs[key] += 1
                        sample.setdefault(key, cells[:12])
                    got = True
            else:
                time.sleep(0.0004)
        time.sleep(0.0004)
    total = sum(sigs.values())
    if total:
        live_cells = {}
        for (head, off, nlive), cnt in sigs.items():
            live_cells[f"head=0x{head:02X} off={off} live={nlive}"] = cnt
        print(f"sub={sub:2d}: {total:4d} frames  {live_cells}")
        for (head, off, nlive), cells in sample.items():
            print(f"         head=0x{head:02X} off={off}: {cells[:12]}")
    else:
        print(f"sub={sub:2d}: no frames")
d.close()
print("DONE")
