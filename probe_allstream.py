"""ALL-stream W probe — 60 s window.

Polls EVERY Hall sensor stream (sub 6 banks A/B, sub 5 banks A/B,
sub 11 banks A/B) synchronously and logs a snapshot of all ~126 cells +
the OS-held WASD set every ~150 ms to wasd_allstream.jsonl.  Also logs a
prominent line the moment W is OS-held.  Press W (hold a few s) during the
window.  After it stops, I find which (sub, bank, idx) cell drops.
"""
import json, os, sys, time, statistics
sys.path.insert(0, "app")
from slice_capture import (find_interfaces, VendorStream, OsKeys,
                           ADC_CELLS_PER_HALF, SUB_READ_ADC,
                           SUB_READ_ADC_ALT, SUB_READ_RAW,
                           CMD_RM6X21, build_cmd)

JL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_allstream.jsonl")
N = ADC_CELLS_PER_HALF
open(JL, "w").close()
ifs = find_interfaces()
ven = VendorStream(ifs["vendor"])
print("vendor open:", ven.open(), flush=True)
osk = OsKeys()
time.sleep(1.2)
print(f"fw={ven.fw_info!r}", flush=True)

# cycle: (sub, bank) ; bank = our request-half label 1/2
CYCLE = [(SUB_READ_ADC, 1), (SUB_READ_ADC, 2),
         (SUB_READ_ADC_ALT, 1), (SUB_READ_ADC_ALT, 2),
         (SUB_READ_RAW, 1), (SUB_READ_RAW, 2)]

import hid
dev = ven._dev

def ask(sub, half):
    dev.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
    deadline = time.time() + 0.010
    while time.time() < deadline:
        r = dev.read(65)
        if r:
            b = ven._norm(r)
            if b and b[0] == 0x5C and b[2] == 0x92 and b[5] == sub:
                cells = [int.from_bytes(b[6 + 2*i:8 + 2*i], "little")
                         for i in range(min((len(b) - 6)//2, N))]
                return cells
        time.sleep(0.0003)
    return None

t_end = time.time() + 60.0
rows = []
last_w = False
print("=== 60s ALL-STREAM WINDOW — press & HOLD W a few seconds ===", flush=True)
with open(JL, "a") as jl:
    while time.time() < t_end:
        t0 = time.time()
        snap = {}
        c = 0
        while time.time() - t0 < 0.05 and c < len(CYCLE) * 3:
            sub, bank = CYCLE[c % len(CYCLE)]
            cells = ask(sub, bank)
            if cells is not None:
                base = (bank - 1) * N
                for i, v in enumerate(cells):
                    snap[f"s{sub}_{bank}_{i}"] = v
            c += 1
        held = sorted(osk.poll())
        rec = {"t": round(time.time(), 3), "os": held, "raw": snap}
        jl.write(json.dumps(rec) + "\n")
        rows.append(rec)
        if "W" in held and not last_w:
            print(f"  >>> W held! at {time.strftime('%H:%M:%S')}", flush=True)
        last_w = "W" in held
        time.sleep(0.1)
        # progress every ~10s
        if int(t_end - time.time()) % 10 < 1:
            print(f"  ... {int(t_end - time.time())}s left", flush=True)

ven.close()
print(f"\nstopped {time.strftime('%H:%M:%S')} rows={len(rows)}", flush=True)
wrows = [r for r in rows if "W" in r["os"]]
rest = [r for r in rows if not r["os"]]
print(f"W-held rows: {len(wrows)}  rest rows: {len(rest)}", flush=True)
if wrows and rest:
    base = {}
    for r in rest:
        for k, v in r["raw"].items():
            if v is not None:
                base.setdefault(k, []).append(v)
    base = {k: statistics.median(v) for k, v in base.items() if v}
    movers = []
    for k in base:
        d = [base[k] - r["raw"][k] for r in wrows if r["raw"].get(k) is not None]
        if d:
            movers.append((k, round(statistics.median(d), 1), round(max(d), 1), len(d)))
    movers.sort(key=lambda x: -x[1])
    print("TOP MOVERS during W-hold (cell, median, max, n):", flush=True)
    for m in movers[:12]:
        print(f"   {m[0]}: median={m[1]} max={m[2]} n={m[3]}", flush=True)
else:
    print("no W or no rest rows — window missed", flush=True)
