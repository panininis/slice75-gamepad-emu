"""Definitive frame probe:
  1) For each (sub, half): send ONE request, count ALL reports (1s window),
     dump each.
  2) Then poll sub-6 (ADC) at 12 Hz for 30 s.  HOLD W for a few seconds
     during this window — the probe prints which ADC position moves.
Closes the adjusting session on exit.
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

print("=== per-request report counts ===")
for sub, half in ((2, 1), (2, 2), (3, 1), (3, 2), (6, 1), (6, 2)):
    dev.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
    reps = drain(1.0)
    print(f"sub={sub} half={half}: {len(reps)} reports")
    for i, r in enumerate(reps[:4]):
        b = r[1:]
        print(f"   [{i}] n={len(r)} h={b[0]:#x} sub={b[4]} "
              f"data={b[5:21].hex(' ')}  ...{b[-12:].hex(' ')}")
    time.sleep(0.15)

# ---------------- ADC sweep with keypress detection ----------------
print("\n=== 30s ADC(6) sweep — HOLD W for a few seconds now ===")
t_end = time.time() + 30
baseline: dict[tuple[int, int], int] = {}
moved: list[str] = []
counts: dict[int, int] = {}
while time.time() < t_end:
    for half in (1, 2):
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (6, half)))
        reps = drain(0.04)
        for r in reps:
            b = r[1:]
            if len(b) < 5 or b[4] != 6:
                continue
            data = b[5:5 + 54]          # up to 27 uint16 per report
            counts[half] = max(counts.get(half, 0), len(data) // 2)
            for i in range(len(data) // 2):
                key = (half, i)
                v = int.from_bytes(data[i * 2:i * 2 + 2], "little")
                if key in baseline:
                    if abs(v - baseline[key]) > 150 and \
                            f"{key}:{v}" not in moved:
                        moved.append(f"half{half}[{i}]={v}")
                else:
                    baseline[key] = v
    time.sleep(0.04)

dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ()))
drain(0.3)
dev.close()
print(f"baseline positions seen: {len(baseline)}")
print("nonzero-ish baseline sample:",
      {p: v for p, v in list(baseline.items())[:10]})
print("positions that moved >150 from baseline:",
      moved if moved else "NONE (no keypress captured)")
print("adjusting session closed")
