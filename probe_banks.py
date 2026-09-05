"""Enumerate ALL distinct sensor banks in the RM6X21 continuous stream.

Send RM6X21 reads continuously for 20 s, capture every response frame,
and cluster frames into distinct banks by their idle-value signature
(head byte + first data cells). Reveals how many banks the board
actually cycles through and their idle signatures, so we can see
whether a bank we haven't been capturing exists (where W would live).
"""
import sys, time, statistics
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); time.sleep(0.3)

def send(sub=6, half=1):
    d.write(build_cmd(CMD_RM6X21, (sub, half)))

frames = []
t0 = time.time()
order = 0
while time.time() - t0 < 20.0:
    send()
    for _ in range(30):
        b = d.read(65)
        if b and len(b) >= 12:
            frames.append(b)
            break
        time.sleep(0.0005)
    order += 1

print("captured frames:", len(frames))

# Parse each frame: head byte + uint16 cells from byte 6
def parse(b):
    head = b[0]
    cells = []
    for j in range(6, min(len(b)-1, 6+2*30), 2):
        cells.append(int.from_bytes(b[j:j+2], "little"))
    return head, cells

# Cluster by signature: (head, rounded first 8 cells)
from collections import OrderedDict
clusters = OrderedDict()
for b in frames:
    head, cells = parse(b)
    # signature = head + first 8 cells rounded to 25 units
    sig = (head, tuple(round(c/25.0) for c in cells[:8]))
    clusters.setdefault(sig, []).append(cells)

print("distinct bank signatures:", len(clusters))
print("=" * 60)
for i, (sig, lst) in enumerate(clusters.items()):
    head = sig[0]
    # representative = median of each cell across the cluster
    import statistics as st
    n = max(len(c) for c in lst)
    med = []
    for k in range(n):
        vals = [c[k] for c in lst if k < len(c)]
        med.append(int(st.median(vals)))
    active = [v for v in med if v > 1000]
    print(f"bank[{i}] head=0x{head:02X}  frames={len(lst)}  active_cells={len(active)}  total={n}")
    print(f"    full median: {med}")
d.close()
print("done")
