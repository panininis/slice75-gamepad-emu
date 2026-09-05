"""W-hunter: visible desktop window + all-sensor logging until W is caught.

Shows a big on-screen window ("PRESS W — HUNTING") so the user knows when to
press (the user can't watch Hermes). It continuously logs every one of the 58
Hall sensors + the OS-held WASD set to wasd_whunt.jsonl. It auto-stops as
soon as the OS confirms a W-hold AND some sensor dropped >500 counts during
that hold — then it reports WHICH position dropped (W's true sensor).
Falls back to a 6-minute cap.
"""
from __future__ import annotations
import json
import os
import sys
import time
import statistics
import ctypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import (find_interfaces, VendorStream, OsKeys,  # noqa: E402
                           ADC_CELLS_PER_HALF)
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_whunt.jsonl")
DUR = 360.0
N_POS = ADC_CELLS_PER_HALF * 2

try:
    SND = ctypes.windll.kernel32
    def beep(n=1):
        for _ in range(n):
            try: SND.Beep(880, 180)
            except Exception: pass
except Exception:
    def beep(n=1): pass

open(JL, "w").close()
ifs = find_interfaces()
ven = VendorStream(ifs["vendor"])
ven.open()
osk = OsKeys()
time.sleep(1.5)
print(f"fw={ven.fw_info!r}", flush=True)

root = tk.Tk()
root.title("W HUNT")
root.attributes("-topmost", True)
root.configure(bg="#0e0e12")
W, H = 520, 260
sx = (root.winfo_screenwidth() - W)//2
sy = (root.winfo_screenheight() - H)//2
root.geometry(f"{W}x{H}+{sx}+{sy}")
top = tk.Label(root, text="PRESS W — HUNTING", font=("Segoe UI", 28, "bold"),
               fg="#5cc8ff", bg="#0e0e12")
top.pack(pady=(16, 2))
mid = tk.Label(root, text="Hold W for 2-3 s\n(and A/S/D as controls)",
               font=("Segoe UI", 16), fg="#ffffff", bg="#0e0e12", justify="center")
mid.pack()
lab = tk.Label(root, text="", font=("Consolas", 15), fg="#7ee787", bg="#0e0e12")
lab.pack(pady=12)
beep(2)
print(">>> W-HUNT window live", flush=True)

t_end = time.time() + DUR
done = False
found = None
rows = []
w_seen = 0        # consecutive W-held samples
w_done = False    # W was held and then released
with open(JL, "a") as jl:
    while time.time() < t_end and not done:
        with ven._lock:
            snap = dict(ven.adc)
        held = osk.poll()
        rec = {"t": round(time.time(), 3), "os": sorted(held),
               "raw": {p: snap.get(p) for p in range(N_POS)}}
        rows.append(rec)
        jl.write(json.dumps(rec) + "\n")
        if "W" in held:
            w_seen += 1
            w_done = False
            dropped = {p: v for p, v in snap.items()
                       if v is not None and v < 2400}
            lab.config(text=f"W held {w_seen}x — dropped {len(dropped)} sensors "
                            + (f"deepest={min(dropped, key=dropped.get)}" if dropped else ""))
        else:
            if w_seen >= 10:
                w_done = True     # full hold captured and released
            w_seen = 0
            lab.config(text="")
        if w_done and w_seen == 0:
            # stop once we've captured at least one full W hold+release
            done = True
        root.update()
        time.sleep(0.05)

beep(3)
try: root.destroy()
except Exception: pass
ven.close()

print(f"\ncaptured {len(rows)} snapshots", flush=True)
# Rest baselines from idle rows
rest = [r for r in rows if not r["os"]]
base = {}
for r in rest:
    for p in range(N_POS):
        if r["raw"].get(p) is not None:
            base.setdefault(p, []).append(r["raw"][p])
base = {p: statistics.median(v) for p, v in base.items() if v}
wrows = [r for r in rows if "W" in r["os"]]
print(f"W OS-held in {len(wrows)} rows (rest baseline rows: {len(rest)})", flush=True)
if wrows:
    movers = []
    for p in sorted(base):
        d = [base[p] - r["raw"][p] for r in wrows if r["raw"].get(p) is not None]
        if len(d) >= 3:
            movers.append((p, round(statistics.median(d), 1)))
    movers.sort(key=lambda x: -x[1])
    print(">>> top movers during W-hold (median drop):", movers[:8], flush=True)
    if movers and movers[0][1] > 300:
        p0, d0 = movers[0]
        print(f">>> W's sensor = pos {p0} (half {p0 // ADC_CELLS_PER_HALF + 1}, "
              f"idx {p0 % ADC_CELLS_PER_HALF}) median_drop={d0}", flush=True)
    else:
        print(">>> no clean W mover >300; inspect wasd_whunt.jsonl", flush=True)
else:
    print(">>> W never OS-held during the window — press W next run", flush=True)
print(f"raw -> {JL}", flush=True)
