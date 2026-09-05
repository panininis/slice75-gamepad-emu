"""Zero-filter raw response framing probe.

Opens the vendor interface ONCE, then for each request (sub6 h1, sub6 h2,
sub2 h1, sub3) logs EVERY report byte-for-byte for 1.5s.
One process, strictly sequential.
"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device()
d.open_path(ifs["vendor"])
d.set_nonblocking(True)
time.sleep(0.3)
d.write(b"\x00" + build_sync())
time.sleep(0.5)
while d.read(65):
    pass

def probe(label, sub, half, seconds=1.5):
    try:
        d.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
    except OSError as e:
        print(f"{label}: write FAILED {e}")
        return
    t0 = time.time()
    reps = []
    while time.time() - t0 < seconds:
        try:
            rep = d.read(65)
        except OSError:
            break
        if rep:
            reps.append((round(time.time() - t0, 4), bytes(rep)))
        else:
            time.sleep(0.0004)
    print(f"\n===== {label}: {len(reps)} reports =====", flush=True)
    for t, b in reps:
        print(f" t+{t:5.3f} len={len(b)} " +
              " ".join(f"{x:02X}" for x in b[:72]), flush=True)
    time.sleep(0.4)

probe("sub6 half1", 6, 1)
probe("sub6 half2", 6, 2)
probe("sub2 half1", 2, 1)
probe("sub3", 3, 1)
print("\ndone", flush=True)
d.close()
