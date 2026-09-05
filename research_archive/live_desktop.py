"""Desktop-visible WASD analog recorder — solves the coordination problem.

Shows a BIG, always-on-top window on the user's actual desktop saying
"PRESS W A S D — RECORDER LIVE" with a pulsing dot and the live stick
values, while it continuously logs the raw Hall-ADC timeline to disk. The
user sees the window and presses at leisure (hold each 2-3 s); no need to
watch Hermes or time anything to a response. Beeps on start and on finish.

Auto-exits as soon as all of W/A/S/D show a clean full-travel swing >0.7,
or after ~12 min. Writes wasd_live_final.jsonl (per-sample) and prints a
per-key peak summary + verdict.
"""
from __future__ import annotations
import json
import os
import sys
import time
import ctypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import (find_interfaces, read_key_value,  # noqa: E402
                           Mapping, VendorStream, OsKeys)
from gamepad_bridge import GamepadBridge, GamepadConfig  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_live_final.jsonl")
DUR = 720.0
THRESH = 0.7

# WinPlaySound for a start/finish beep (no audio file needed)
try:
    SND = ctypes.windll.kernel32
    def beep(n=1):
        try:
            for _ in range(n):
                SND.Beep(880, 180)
        except Exception:
            pass
except Exception:
    def beep(n=1):
        pass

open(JL, "w").close()
ifs = find_interfaces()
print(f"interfaces: {list(ifs)}", flush=True)
if "vendor" not in ifs:
    print("vendor not found"); sys.exit(2)
ven = VendorStream(ifs["vendor"])
print("vendor open:", ven.open(), flush=True)
osk = OsKeys()
time.sleep(1.2)
print(f"fw={ven.fw_info!r} frames={ven.frame_count}", flush=True)
mp = Mapping()
POS = {k: mp.adc_pos.get(k, Mapping.DEFAULT_ADC_MAP[k]) for k in "WASD"}
bridge = GamepadBridge(GamepadConfig(normalize_mode="full"))
bridge.open()
print(f"adc positions: {POS}", flush=True)

import tkinter as tk
root = tk.Tk()
root.title("SLICE-PAD RECORDER")
root.attributes("-topmost", True)
root.configure(bg="#0e0e12")
W, H = 560, 300
sx = (root.winfo_screenwidth() - W) // 2
sy = (root.winfo_screenheight() - H) // 2
root.geometry(f"{W}x{H}+{sx}+{sy}")
root.overrideredirect(True)

top = tk.Label(root, text="RECORDER LIVE", font=("Segoe UI", 26, "bold"),
               fg="#ffd166", bg="#0e0e12")
top.pack(pady=(18, 2))
mid = tk.Label(root, text="Press W, A, S, D now\n(hold each 2-3 s, one at a time)",
               font=("Segoe UI", 17), fg="#ffffff", bg="#0e0e12", justify="center")
mid.pack()
dot = tk.Label(root, text="\u25CF", font=("Segoe UI", 22), fg="#ff5c5c", bg="#0e0e12")
dot.pack(pady=6)
doton = False
valfr = tk.Frame(root, bg="#0e0e12")
valfr.pack(pady=8)
vals = {}
for k in "WASD":
    v = tk.Label(valfr, text=f"{k}  0.00", font=("Consolas", 22, "bold"),
                 fg="#7ee787", bg="#0e0e12")
    v.grid(row=0, column="WASD".index(k), padx=10, pady=4)
    vals[k] = v

beep(2)
print(">>> WINDOW LIVE — user should see 'RECORDER LIVE' on desktop", flush=True)

peak = {k: 0.0 for k in "WASD"}
t_end = time.time() + DUR
done = False
with open(JL, "a") as jl:
    while time.time() < t_end and not done:
        vw = read_key_value(mp, "W", ven, None)
        va = read_key_value(mp, "A", ven, None)
        vs = read_key_value(mp, "S", ven, None)
        vd = read_key_value(mp, "D", ven, None)
        vals_ = {"W": vw, "A": va, "S": vs, "D": vd}
        for k in "WASD":
            peak[k] = max(peak[k], vals_[k])
        x16, y16 = bridge.update(vw, va, vs, vd)
        raw = {k: ven.adc.get(POS[k]) for k in "WASD"}
        jl.write(json.dumps({"t": round(time.time(), 3), "os": sorted(osk.poll()),
                             "raw": raw, "trv": {k: round(vals_[k], 3) for k in "WASD"},
                             "stk": [x16, y16]}) + "\n")
        # UI
        doton = not doton
        dot.config(fg="#ff5c5c" if doton else "#5c2a2a")
        for k in "WASD":
            c = vals_[k]
            vals[k].config(text=f"{k}  {c:.2f}",
                           fg="#ffd166" if c > THRESH else "#7ee787")
        if all(peak[k] > THRESH for k in "WASD"):
            done = True
        root.update()
        time.sleep(0.12)

beep(3)
try:
    root.destroy()
except Exception:
    pass
ven.close()
bridge.close()
print("\n=== PEAK TRAVEL PER KEY ===", flush=True)
allok = True
for k in "WASD":
    good = peak[k] > THRESH
    allok = allok and good
    print(f"  {k}: pos={POS[k]} peak={peak[k]:.2f} [{'OK' if good else 'low'}]", flush=True)
print(f"\nVERDICT: {'PASS — analog WASD->gamepad works end to end' if allok else 'incomplete — see peaks'}", flush=True)
print(f"raw timeline -> {JL}", flush=True)
sys.exit(0 if allok else 1)
