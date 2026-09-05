"""Diagnostic WASD analog recorder — full raw timeline, zero timing needed.

For up to ~30 min (or as soon as all of W/A/S/D show a clean full-travel
swing >0.7), it logs to wasd_final.jsonl EVERY ~120 ms:
   * os  : the OS-held WASD set (GetAsyncKeyState, correct VK codes)
   * raw : raw Hall ADC for the four mapped sensors
   * trv : normalized travel 0..1 for the four
   * stk : the 16-bit stick the bridge produces

This captures the ANALOG swing regardless of OS press timing, so a press at
any point in the window is visible in the raw trace. The user presses
W/A/S/D at their convenience (hold each 2-3 s, a few times) — nothing to
watch or time.

At the end it reports the peak travel per key and whether the analog path
is working.
"""
from __future__ import annotations
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import (find_interfaces, read_key_value,  # noqa: E402
                           Mapping, VendorStream, OsKeys)
from gamepad_bridge import GamepadBridge, GamepadConfig  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_final.jsonl")
DUR = 2400.0
THRESH = 0.7

open(JL, "w").close()
ifs = find_interfaces()
print(f"interfaces: {list(ifs)}", flush=True)
if "vendor" not in ifs:
    print("vendor not found"); sys.exit(2)
ven = VendorStream(ifs["vendor"])
print("vendor open:", ven.open(), flush=True)
osk = OsKeys()
print(f"OsKeys.available={osk.available}", flush=True)
time.sleep(1.5)
print(f"fw={ven.fw_info!r} frames={ven.frame_count}", flush=True)
mp = Mapping()
POS = {k: mp.adc_pos.get(k, Mapping.DEFAULT_ADC_MAP[k]) for k in "WASD"}
print("adc positions:", POS, flush=True)
bridge = GamepadBridge(GamepadConfig(normalize_mode="full"))
ok, msg = bridge.open()
print(f"bridge: {msg}", flush=True)

print(f"\n=== recorder live — press W/A/S/D at your convenience "
      f"(hold each 2-3 s); exits early when all 4 swing ===", flush=True)

peak = {k: 0.0 for k in "WASD"}
t_end = time.time() + DUR
last_hb = 0.0
done = False

with open(JL, "a") as jl:
    while time.time() < t_end and not done:
        vw = read_key_value(mp, "W", ven, None)
        va = read_key_value(mp, "A", ven, None)
        vs = read_key_value(mp, "S", ven, None)
        vd = read_key_value(mp, "D", ven, None)
        vals = {"W": vw, "A": va, "S": vs, "D": vd}
        for k in "WASD":
            if vals[k] > peak[k]:
                peak[k] = vals[k]
        x16, y16 = bridge.update(vw, va, vs, vd)
        raw = {k: ven.adc.get(POS[k]) for k in "WASD"}
        rec = {"t": round(time.time(), 3),
               "os": sorted(osk.poll()),
               "raw": {k: raw[k] for k in "WASD"},
               "trv": {k: round(vals[k], 3) for k in "WASD"},
               "stk": [x16, y16]}
        jl.write(json.dumps(rec) + "\n")
        # instant, prominent marker the moment a real press is seen
        moved = [k for k in "WASD" if (raw[k] is not None and raw[k] < 2100)]
        if moved:
            print(f"  >>> PRESS {''.join(sorted(moved))}  "
                  + " ".join(f"{k}={raw[k]}" for k in "WASD")
                  + "  trv=" + " ".join(f"{k}={vals[k]:.2f}" for k in "WASD")
                  + f"  stk=({x16:+5d},{y16:+5d})", flush=True)
        # early exit once every key has swung
        if all(peak[k] > THRESH for k in "WASD"):
            done = True
        now = time.time()
        if now - last_hb >= 10.0:
            last_hb = now
            print(f"  ... {int(t_end-now)}s left  peak "
                  f"W={peak['W']:.2f} A={peak['A']:.2f} "
                  f"S={peak['S']:.2f} D={peak['D']:.2f}  frames={ven.frame_count}",
                  flush=True)
        time.sleep(0.12)

ven.close()
bridge.close()
print("\n=== PEAK TRAVEL PER KEY ===", flush=True)
allok = True
for k in "WASD":
    good = peak[k] > THRESH
    allok = allok and good
    print(f"  {k}: pos={POS[k]} peak={peak[k]:.2f} [{'OK' if good else 'low'}]", flush=True)
print(f"\nVERDICT: {'PASS — analog path works' if allok else 'incomplete — see peaks'}", flush=True)
print(f"raw timeline -> {JL}", flush=True)
