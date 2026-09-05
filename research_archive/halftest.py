"""Decisive: does the `half` parameter select a different frame type?

Request sub=6 half=1 x30, dump distinct patterns.
Request sub=6 half=2 x30, dump distinct patterns.
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

def stream(half, n=30):
    pats = {}
    order = []
    t0 = time.time()
    while len(order) < n:
        d.write(b"\x00" + build_cmd(CMD_RM6X21, (6, half)))
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
                sig = tuple(0 if c == 0 else 1 for c in cells)
                order.append(sig)
                if sig not in pats:
                    pats[sig] = cells
                break
            time.sleep(0.0004)
        time.sleep(0.0003)
    return pats, order

for half in (1, 2):
    pats, order = stream(half)
    print(f"=== half={half}: {len(order)} frames, {len(pats)} distinct pattern(s)")
    for sig, cells in pats.items():
        dead = [i for i, c in enumerate(cells) if c == 0]
        print(f"  pattern dead-idx={dead}")
        print(f"    cells={cells}")
    counts = collections.Counter(order)
    print(f"  pattern counts: {dict(counts)}")
    print()
    time.sleep(0.5)

d.close()
