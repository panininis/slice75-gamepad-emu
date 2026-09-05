"""Raw diagnostic: after writing RM6X21, log EVERY byte of EVERY read for
~6 s with no filtering. Reveals whether the board is silent or answering
in a format we've been dropping.
"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,
                           CMD_RM6X21)

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); d.set_nonblocking(True)
time.sleep(0.3)

def dump(label, secs=3.0):
    t0 = time.time()
    n = 0
    first = []
    while time.time() - t0 < secs:
        try:
            rep = d.read(65)
        except OSError:
            time.sleep(0.002); continue
        if rep:
            b = bytes(rep)
            n += 1
            if len(first) < 6:
                first.append(b[:12].hex())
            time.sleep(0.001)
        else:
            time.sleep(0.001)
    print(f"[{label}] reads={n} first={first}")

# baseline idle
dump("idle (no writes)")

# SYNC
d.write(b"\x00" + build_sync())
time.sleep(0.2)
dump("after SYNC", 2.0)

# RM6X21 sub6
for _ in range(50):
    d.write(build_cmd(CMD_RM6X21, (6, 1)))
    time.sleep(0.002)
dump("after 50x RM6X21(6,1)", 3.0)

# sub2 (web-driver read sub)
for _ in range(50):
    d.write(build_cmd(CMD_RM6X21, (2, 1)))
    time.sleep(0.002)
dump("after 50x RM6X21(2,1)", 3.0)

d.close()
print("done")
