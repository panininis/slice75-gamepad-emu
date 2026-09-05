"""Live end-to-end test of the real analog path.

Runs the ACTUAL engine pieces: VendorStream (ADC) + read_key_value +
GamepadBridge.  For 60 s it prints the live W/A/S/D travel and the
resulting 16-bit stick.  You press the keys normally; the stick must
follow.  This is the ground-truth "does the app work" check.

No GUI.  Prints a live line ~4x/sec.
"""
from __future__ import annotations
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import (find_interfaces, read_key_value,  # noqa: E402
                           Mapping, VendorStream, OsKeys)
oskeys = OsKeys()
from gamepad_bridge import GamepadBridge, GamepadConfig  # noqa: E402

DUR = 90.0
ifs = find_interfaces()
print(f"interfaces: {list(ifs)}")
if "vendor" not in ifs:
    print("vendor interface not found — is the keyboard connected?"); sys.exit(2)

ven = VendorStream(ifs["vendor"])
print("vendor open:", ven.open())
time.sleep(1.0)
print(f"fw={ven.fw_info!r} frames so far={ven.frame_count}")
mp = Mapping()
for k in "WASD":
    pos = mp.adc_pos.get(k, Mapping.DEFAULT_ADC_MAP[k])
    print(f"  {k} -> adc pos {pos}")
bridge = GamepadBridge(GamepadConfig(normalize_mode="full"))
ok, msg = bridge.open()
print(f"bridge: {msg}")

print(f"\n=== {DUR:.0f}s: press W/A/S/D (hold each 2s, one at a time) ===", flush=True)
t_end = time.time() + DUR
last = 0.0
maxmag = 0.0
while time.time() < t_end:
    vw = read_key_value(mp, "W", ven, None)
    va = read_key_value(mp, "A", ven, None)
    vs = read_key_value(mp, "S", ven, None)
    vd = read_key_value(mp, "D", ven, None)
    x16, y16 = bridge.update(vw, va, vs, vd)
    now = time.time()
    if now - last >= 0.25:
        last = now
        mag = (x16*x16 + y16*y16) ** 0.5 / 32767
        maxmag = max(maxmag, mag)
        os_h = sorted(oskeys.poll())
        print(f"  W={vw:.2f} A={va:.2f} S={vs:.2f} D={vd:.2f}  "
              f"stick=({x16:+6d},{y16:+6d}) mag={mag:.2f}  "
              f"os={os_h or '-'}  [frames={ven.frame_count}]")
ven.close()
bridge.close()
print(f"\ndone — max stick magnitude seen: {maxmag:.2f}")
print("RESULT:", "PASS (stick moved)" if maxmag > 0.2 else
      "no significant stick motion — keys not detected?")
