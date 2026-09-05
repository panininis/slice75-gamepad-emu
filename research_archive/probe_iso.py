"""Isolated analog-source probe (NO vendor stream running, to avoid
interference).  For 60 s it logs, timestamped:
   * OS key state (GetAsyncKeyState W/A/S/D)
   * MI_00 6-byte analog block changes
so we can see (a) whether Windows sees the physical WASD, and (b) whether
the MI_00 analog block moves on a real keypress.
Just hold each of W, A, S, D for 2-3 s somewhere in the 60 s window.
"""
from __future__ import annotations
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import HidAnalog, find_interfaces  # noqa: E402

user32 = ctypes.windll.user32
VK = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20}
W, A, S, D = [user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x11, 0x1E, 0x1F, 0x20)]


def os_state() -> set[str]:
    return {k for k, vk in VK.items() if user32.GetAsyncKeyState(vk) & 0x8000}


ifs = find_interfaces()
print("interfaces:", list(ifs))
hid = HidAnalog(ifs["kbd"]) if "kbd" in ifs else None
if hid:
    hid.open()
print("MI_00 open:", bool(hid and hid.opened))
print("=== 60s window: hold W, then A, then S, then D (2-3s each) ===")
t0 = time.time()
t_end = t0 + 60
events = []
last = None
os_seen: set[str] = set()
while time.time() < t_end:
    if hid:
        hid.poll()
    st = os_state()
    os_seen |= st
    key = (tuple(hid.latest) if hid else (), tuple(sorted(st)))
    if key != last:
        last = key
        el = time.time() - t0
        events.append((el, hid.latest if hid else [], sorted(st)))
        print(f"  t={el:5.2f}  MI00={list(hid.latest) if hid else '-'}  os={sorted(st)}")
    time.sleep(0.001)
if hid:
    hid.close()
print("\n=== SUMMARY ===")
print("OS ever saw held keys:", sorted(os_seen) or "NONE")
mi00_moves = [e for e in events if e[1] and any(v != 0 for v in e[1])]
print(f"MI_00 non-zero states: {len(mi00_moves)}")
for el, block, st in mi00_moves[:12]:
    nz = {i: v for i, v in enumerate(block) if v}
    print(f"  t={el:5.2f} os={st} nonzero_ch={nz}")
if not os_seen:
    print(">>> OS never registered any WASD — the board may route WASD as analog-only.")
if not mi00_moves:
    print(">>> MI_00 block never moved — not the per-key analog source.")
