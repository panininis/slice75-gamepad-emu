"""Sub-sweep v2: for each sub 0..15, request it 60x, collect ALL distinct
response heads + cell signatures. Goal: find which request makes the rare
0x4A bank (19 live cells) stream, so we can force it during a W press.
"""
import sys, time, collections
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); d.set_nonblocking(True)
time.sleep(0.3)

for sub in range(0, 16):
    heads = collections.Counter()
    sigs = {}
    for _ in range(60):
        d.write(build_cmd(CMD_RM6X21, (sub, 1)))
        got = False
        deadline = time.time() + 0.008
        while time.time() < deadline and not got:
            try:
                rep = d.read(65)
            except OSError:
                break
            if rep:
                b = bytes(rep)
                if len(b) == 65 and b[0] == 0:
                    b = b[1:]
                if len(b) >= 12 and b[0] != 0x00 and b[2] == 0x92:
                    h = b[0]
                    heads[h] += 1
                    cells = tuple(int.from_bytes(b[6 + 2*i:8 + 2*i], "little")
                                  for i in range(4))
                    sigs.setdefault(h, cells)
                    got = True
            time.sleep(0.0003)
        time.sleep(0.0003)
    if heads:
        out = {f"0x{h:02X}": (c, sigs[h][:3]) for h, c in heads.items()}
        print(f"sub={sub:2d}: {out}")
    else:
        print(f"sub={sub:2d}: no 0x92 response")
d.close()
print("done")
