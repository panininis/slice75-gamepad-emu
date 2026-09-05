"""Long self-contained WASD -> analog capture. NO coordination needed.

Opens the vendor ADC stream + OsKeys and logs (every ~120 ms) for ~4 min:
  * OS-held WASD (GetAsyncKeyState, correct VK codes)  -> ground truth
  * analog travel 0..1 for the four mapped sensors
  * 16-bit stick the bridge produces

You press W/A/S/D freely the whole time — nothing to watch. A "HELD" line
prints whenever the OS confirms a key. At the end it reports, per key, the
peak analog travel observed while the OS confirmed that key was held.

This is the decisive test that the app's analog -> gamepad path works.
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

DUR = 240.0
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

print(f"\n=== {DUR:.0f}s CAPTURE — press W/A/S/D freely, as often as you like ===", flush=True)
peak = {k: 0.0 for k in "WASD"}
os_ticks = {k: 0 for k in "WASD"}
os_any = 0
maxmag = 0.0
events = []
t_end = time.time() + DUR
last = 0.0
prev_os: set[str] = set()
while time.time() < t_end:
    vw = read_key_value(mp, "W", ven, None)
    va = read_key_value(mp, "A", ven, None)
    vs = read_key_value(mp, "S", ven, None)
    vd = read_key_value(mp, "D", ven, None)
    vals = {"W": vw, "A": va, "S": vs, "D": vd}
    x16, y16 = bridge.update(vw, va, vs, vd)
    mag = (x16*x16 + y16*y16) ** 0.5 / 32767
    maxmag = max(maxmag, mag)
    held = osk.poll()
    for k in "WASD":
        if k in held:
            os_ticks[k] += 1
            if vals[k] > peak[k]:
                peak[k] = vals[k]
    if held:
        os_any += 1
        if held != prev_os:
            tag = " ".join(f"{k}={vals[k]:.2f}" for k in sorted(held))
            print(f"  HELD {sorted(held)}  {tag}  stick=({x16:+5d},{y16:+5d})", flush=True)
            events.append({"os": sorted(held),
                           "vals": {k: round(vals[k], 3) for k in held},
                           "stick": [x16, y16]})
    prev_os = held
    now = time.time()
    if now - last >= 1.0:
        last = now
        print(f"  ... {int(t_end-now):3d}s left  "
              f"peak W={peak['W']:.2f} A={peak['A']:.2f} S={peak['S']:.2f} "
              f"D={peak['D']:.2f}  os_ticks={os_ticks}", flush=True)
    time.sleep(0.12)

ven.close()
bridge.close()
print("\n=== CORRELATION (peak analog while OS-confirmed-held) ===", flush=True)
table = {}
for k in "WASD":
    confirmed = os_ticks[k] > 0
    table[k] = {"os_ticks": os_ticks[k], "peak": round(peak[k], 3),
                "confirmed": confirmed, "adc_pos": POS[k]}
    status = "OK" if (confirmed and peak[k] > 0.2) else (
        "no-analog" if confirmed else "not-pressed")
    print(f"  {k}: pos={POS[k]} os_ticks={os_ticks[k]:4d} "
          f"peak={peak[k]:.2f} [{status}]", flush=True)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "wasd_live2.json"), "w") as f:
    json.dump({"table": table, "os_any": os_any, "max_mag": round(maxmag, 3),
               "events": events}, f, indent=2)
print("saved wasd_live2.json", flush=True)
pressed = [k for k in "WASD" if os_ticks[k] > 0]
mapped = [k for k in pressed if peak[k] > 0.2]
print(f"\nOS saw: {pressed or 'NONE'}", flush=True)
if not pressed:
    print("VERDICT: no key events reached Windows during the window — press "
          "again when I next run a capture.", flush=True)
elif mapped:
    print(f"VERDICT: PASS — {mapped} show analog travel + OS events. "
          f"Analog->gamepad path works. max_mag={maxmag:.2f}", flush=True)
else:
    print("VERDICT: OS saw the keys but analog didn't move — wiring/mapping "
          "issue on the analog side.", flush=True)
