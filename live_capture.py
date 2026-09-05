"""Long-run WASD -> analog correlation capture (NO timing needed).

Runs the REAL app path (VendorStream ADC -> read_key_value -> bridge) for
~5 minutes and continuously logs, every ~100 ms:
   * OS-held WASD keys  (GetAsyncKeyState, correct VK codes)  -> ground truth
   * analog travel 0..1 for the four mapped sensor positions
   * the 16-bit stick the bridge produces

Whenever the OS confirms a WASD key is held it prints a prominent line.
At the end it correlates: for each key, the peak analog travel observed
DURING the windows the OS confirmed that key was held.

You press keys normally and freely the whole time — nothing to watch.
Saves wasd_live.json with the correlation table.
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

DUR = 300.0                      # 5 minutes — press whenever
ifs = find_interfaces()
print(f"interfaces: {list(ifs)}", flush=True)
if "vendor" not in ifs:
    print("vendor interface not found — keyboard connected?"); sys.exit(2)

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

print("\n=== CAPTURE RUNNING ~5 MIN — press W/A/S/D normally, as often as you like ===", flush=True)

# per-key: peak travel while OS-confirmed-held ; count of os-confirmed ticks
peak: dict[str, float] = {k: 0.0 for k in "WASD"}
os_ticks: dict[str, int] = {k: 0 for k in "WASD"}
os_any = 0
maxmag = 0.0
events: list[dict] = []
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
    # prominent line on OS-held transitions
    if held != prev_os:
        tag = " ".join(f"{k}={vals[k]:.2f}" for k in sorted(held) or "WASD")
        print(f"  OS {'HELD '+str(sorted(held)) if held else 'idle':16s} {tag}  "
              f"stick=({x16:+5d},{y16:+5d}) mag={mag:.2f}", flush=True)
        if held:
            events.append({"t": round(time.time(), 2), "os": sorted(held),
                           "vals": {k: round(vals[k], 3) for k in held}})
    prev_os = held
    now = time.time()
    if now - last >= 1.0:          # heartbeat once/sec
        last = now
        rem = int(t_end - now)
        print(f"  ... {rem:3d}s left  peak W={peak['W']:.2f} A={peak['A']:.2f} "
              f"S={peak['S']:.2f} D={peak['D']:.2f}  os_ticks={os_ticks}", flush=True)
    time.sleep(0.1)

ven.close()
bridge.close()

print("\n=== CORRELATION (peak analog travel while OS-confirmed-held) ===", flush=True)
table = {}
for k in "WASD":
    confirmed = os_ticks[k] > 0
    table[k] = {"os_ticks": os_ticks[k], "peak_travel": round(peak[k], 3),
                "confirmed": confirmed,
                "adc_pos": POS[k]}
    status = "OK" if (confirmed and peak[k] > 0.2) else \
             ("no-analog" if confirmed else "not-pressed")
    print(f"  {k}: pos={POS[k]}  os_ticks={os_ticks[k]:4d}  "
          f"peak_travel={peak[k]:.2f}  [{status}]", flush=True)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "wasd_live.json"), "w") as f:
    json.dump({"table": table, "os_any": os_any,
               "max_mag": round(maxmag, 3), "events": events}, f, indent=2)
print("saved wasd_live.json", flush=True)

# verdict
pressed = [k for k in "WASD" if os_ticks[k] > 0]
mapped = [k for k in pressed if peak[k] > 0.2]
print(f"\nOS saw keys: {pressed or 'NONE'}", flush=True)
if not pressed:
    print("VERDICT: OS never saw any WASD — the keyboard is NOT sending these "
          "keys to Windows (check the keyboard mode / that you're pressing the "
          "physical WASD). Analog stream was live, but no key events to correlate.", flush=True)
elif mapped:
    print(f"VERDICT: PASS — {mapped} show analog travel correlated with OS "
          f"key events. The analog->gamepad path works.", flush=True)
else:
    print("VERDICT: OS saw the keys but analog did not move — mapping/wiring "
          "issue on the analog side.", flush=True)
