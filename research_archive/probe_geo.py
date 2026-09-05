"""Geometry + layout probe (no keypresses needed):
  1) print exact byte length of a sub-6 response
  2) send DEFKEY(0) and dump the key-layout response (keyValue per slot)
"""
from __future__ import annotations
import sys
import time

sys.path.insert(0, "app")
import hid  # noqa: E402
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, CMD_START_ADJUSTING,
                           CMD_SAVE_ADJUSTING)

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)


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


dev.write(b"\x00" + build_sync()); drain(0.5)

# 1) exact geometry of one sub-6 response
dev.write(b"\x00" + build_cmd(CMD_RM6X21, (6, 1)))
reps = drain(0.6)
print(f"sub6 half1 -> {len(reps)} reports")
for r in reps:
    print(f"  full-report len={len(r)}  id={r[0]:#x}")
    b = r[1:]
    print(f"  payload len={len(b)}  head={b[:5].hex(' ')}  "
          f"data_bytes={len(b)-5}  uint16_count={(len(b)-5)//2}")
    print(f"  data: {b[5:].hex(' ')}")

# 2) DEFKEY layout
dev.write(b"\x00" + build_cmd(43, (0,)))
reps = drain(0.8)
print(f"\nDEFKEY(0) -> {len(reps)} reports")
for i, r in enumerate(reps):
    b = r[1:]
    print(f"  [{i}] len={len(b)} {b.hex(' ')}")
    if i < 2:
        try:
            txt = b.decode("latin-1", "replace")
            import re
            for m in re.finditer(r"[\x20-\x7e]{4,}", txt):
                print(f"      ascii: {m.group(0)!r}")
        except Exception:
            pass
dev.close()
