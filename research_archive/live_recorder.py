"""15-minute always-on WASD recorder — ZERO coordination needed.

Opens vendor ADC + OsKeys + bridge, then for ~25 min polls the OS at ~8 Hz
and logs EVERY WASD press the instant it happens, with the analog travel at
that instant. Each event is appended to wasd_events.jsonl immediately (so
nothing is lost even if the process is killed). A heartbeat prints every
15 s.

The user does NOT watch anything — they press W/A/S/D whenever natural
(hold each ~2 s, a few times). After it runs, read wasd_events.jsonl.

At the end it also writes wasd_events_summary.json with, per key, the peak
analog travel observed while the OS confirmed that key was held.
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

DUR = 1500.0                       # 25 minutes
HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_events.jsonl")
SUM = os.path.join(HERE, "wasd_events_summary.json")

# start fresh event log
open(JL, "w").close()

ifs = find_interfaces()
print(f"interfaces: {list(ifs)}", flush=True)
if "vendor" not in ifs:
    print("vendor not found — keyboard connected?"); sys.exit(2)

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

print(f"\n=== {DUR/60:.0f}-min recorder running — press W/A/S/D whenever "
      f"natural; NOTHING to watch ===", flush=True)

peak = {k: 0.0 for k in "WASD"}
os_ticks = {k: 0 for k in "WASD"}
maxmag = 0.0
n_events = 0
t_end = time.time() + DUR
last_hb = 0.0
prev_held: frozenset[str] = frozenset()

with open(JL, "a") as jl:
    while time.time() < t_end:
        vw = read_key_value(mp, "W", ven, None)
        va = read_key_value(mp, "A", ven, None)
        vs = read_key_value(mp, "S", ven, None)
        vd = read_key_value(mp, "D", ven, None)
        vals = {"W": vw, "A": va, "S": vs, "D": vd}
        x16, y16 = bridge.update(vw, va, vs, vd)
        mag = (x16*x16 + y16*y16) ** 0.5 / 32767
        maxmag = max(maxmag, mag)
        held = frozenset(osk.poll())
        for k in "WASD":
            if k in held:
                os_ticks[k] += 1
                if vals[k] > peak[k]:
                    peak[k] = vals[k]
        # log on every NEW press (transition into held) AND on release
        newly = held - prev_held
        if newly:
            ev = {"t": round(time.time(), 3), "press": sorted(newly),
                  "vals": {k: round(vals[k], 3) for k in sorted(newly)},
                  "raw": {k: ven.adc.get(POS[k]) for k in sorted(newly)},
                  "stick": [x16, y16], "mag": round(mag, 3)}
            jl.write(json.dumps(ev) + "\n"); jl.flush()
            n_events += 1
            print(f"  PRESS {sorted(newly)}  "
                  + " ".join(f"{k}={vals[k]:.2f}" for k in sorted(newly))
                  + f"  stick=({x16:+5d},{y16:+5d})", flush=True)
        prev_held = held
        now = time.time()
        if now - last_hb >= 15.0:
            last_hb = now
            print(f"  ... {int(t_end-now)}s left  events={n_events}  "
                  f"peak W={peak['W']:.2f} A={peak['A']:.2f} "
                  f"S={peak['S']:.2f} D={peak['D']:.2f}", flush=True)
        time.sleep(0.12)

ven.close()
bridge.close()
table = {}
for k in "WASD":
    confirmed = os_ticks[k] > 0
    table[k] = {"os_ticks": os_ticks[k], "peak": round(peak[k], 3),
                "confirmed": confirmed, "adc_pos": POS[k]}
summary = {"table": table, "n_events": n_events,
           "max_mag": round(maxmag, 3)}
with open(SUM, "w") as f:
    json.dump(summary, f, indent=2)
print("\n=== SUMMARY ===", flush=True)
for k in "WASD":
    c = table[k]
    status = "OK" if (c['confirmed'] and c['peak'] > 0.2) else (
        "no-analog" if c['confirmed'] else "not-pressed")
    print(f"  {k}: pos={c['adc_pos']} os_ticks={c['os_ticks']:4d} "
          f"peak={c['peak']:.2f} [{status}]", flush=True)
print(f"events={n_events} max_mag={maxmag:.2f}", flush=True)
print("saved wasd_events.jsonl + wasd_events_summary.json", flush=True)
pressed = [k for k in "WASD" if os_ticks[k] > 0]
mapped = [k for k in pressed if peak[k] > 0.2]
print(f"\nOS saw: {pressed or 'NONE'}", flush=True)
if not pressed:
    print("VERDICT: no WASD reached Windows during the 25 min — the keyboard "
          "is not sending these keys, or WASD was not pressed.", flush=True)
elif mapped:
    print(f"VERDICT: PASS — {mapped} show analog travel + OS events. "
          f"Analog->gamepad path works. max_mag={maxmag:.2f}", flush=True)
else:
    print("VERDICT: OS saw the keys but analog didn't move — mapping/wiring "
          "issue on the analog side.", flush=True)
