"""Single-request frame capture: send ONE RM6X21 request, then read
everything available for 600 ms.  Repeats for sub 2 / 3 / 6 to map the
exact on-wire response structure (how many reports per request, layout).
"""
from __future__ import annotations
import sys
import time

sys.path.insert(0, "app")
import hid  # noqa: E402
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, SUB_READ_MM, SUB_READ_PRESS,
                           CMD_START_ADJUSTING, CMD_SAVE_ADJUSTING)

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"])
dev.set_nonblocking(True)


def drain(t):
    out = []
    t0 = time.time()
    while time.time() - t0 < t:
        rep = dev.read(65)
        if rep:
            out.append(bytes(rep))
        else:
            time.sleep(0.001)
    return out


dev.write(b"\x00" + build_sync())
drain(0.5)
dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ()))
drain(0.3)

for label, sub in (("MM(2,1)", SUB_READ_MM), ("PRESS(3,1)", SUB_READ_PRESS),
                   ("ADC(6,1)", 6)):
    dev.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, 1)))
    reps = drain(0.6)
    print(f"=== {label}: {len(reps)} reports ===")
    for i, r in enumerate(reps[:8]):
        body = r[1:]
        print(f"  [{i}] len={len(r)} {body.hex(' ')}")
    time.sleep(0.1)
dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ()))
drain(0.3)
dev.close()
print("adjusting session closed")
