"""FINAL W-hunter: reliably stream ALL 4 sensor banks (web-driver request
pattern) and correlate with W held. 90 s.

Banks (all Hall ADC, rest ~2800-3000, drop ~1200 on a press):
  A/B  : sub 6 (head 0x5C), 29 cells, bank by c0 signature
  R1   : sub 11, head 0x5E/0x5F, data from b[2], ~5 live
  R2   : sub 11, head 0x49/0x4A, data from b[2], ~6 live
The web driver's request sequence (sub2,sub2,sub6,sub6 + sub11) makes all
banks stream reliably.  Press W (hold ~5 s) during the window.
"""
import json, os, sys, time, statistics
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,
                           CMD_RM6X21, SUB_READ_MM, SUB_READ_ADC, SUB_READ_RAW,
                           OsKeys)

JL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_whunt3.jsonl")
open(JL, "w").close()
ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"])
dev.set_nonblocking(True)
osk = OsKeys()
dev.write(b"\x00" + build_sync())
time.sleep(0.5)
t0 = time.time()
while time.time() - t0 < 0.6:
    dev.read(65); time.sleep(0.001)

# web-driver request sequence (makes all banks stream)
SEQ = [(SUB_READ_MM, 1), (SUB_READ_MM, 2),
       (SUB_READ_ADC, 1), (SUB_READ_ADC, 2),
       (SUB_READ_RAW, 1)]

def _plausible(vals):
    ok = sum(1 for v in vals if (1800 < v < 3800) or v == 0)
    return ok >= max(3, len(vals) - 1)

def parse(b):
    if len(b) < 12:
        return None
    h = b[0]
    if h == 0x5C and b[2] == 0x92 and b[5] in (6, 5):
        c0 = int.from_bytes(b[6:8], "little")
        bank = "A" if c0 < 2950 else "B"
        n = min((len(b) - 6) // 2, 29)
        return bank, {i: int.from_bytes(b[6 + 2*i:8 + 2*i], "little") for i in range(n)}
    if b[1] == 0x0B and h != 0x5C:
        # R1 data starts at b[2]; R2 data starts at b[3].  Try both, the
        # plausible offset wins.
        for off in (2, 3):
            if len(b) < off + 14:
                continue
            vals = [int.from_bytes(b[off + 2*i:off + 2*i + 2], "little")
                    for i in range(6)]
            if _plausible(vals):
                n = min((len(b) - off) // 2, 9)
                cells = {i: int.from_bytes(b[off + 2*i:off + 2*i + 2], "little")
                         for i in range(n)}
                live = sum(1 for v in cells.values() if v > 1800)
                bank = "R2" if live >= 6 else "R1"
                return bank, cells
    return None

t_end = time.time() + 90.0
rows = []
last_w = False
print("=== 90s FINAL: HOLD W ~5s (all 4 banks, web-driver pattern) ===", flush=True)
last_hb = 0.0
tick = 0
with open(JL, "a") as jl:
    while time.time() < t_end:
        # round-robin the request sequence every ~20 ms
        sub, half = SEQ[tick % len(SEQ)]
        try:
            dev.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
        except OSError:
            pass
        tick += 1
        snap = {}
        t0 = time.time()
        while time.time() - t0 < 0.090:
            r = dev.read(65)
            if r:
                p = parse(bytes(r))
                if p:
                    bank, cells = p
                    for i, v in cells.items():
                        snap[f"{bank}_{i}"] = v
            else:
                time.sleep(0.0004)
        held = sorted(osk.poll())
        rec = {"t": round(time.time(), 3), "os": held, "raw": snap}
        jl.write(json.dumps(rec) + "\n")
        rows.append(rec)
        if "W" in held and not last_w:
            print(f"  >>> W held @ {time.strftime('%H:%M:%S')} (cells={len(snap)})", flush=True)
        last_w = "W" in held
        if time.time() - last_hb >= 15:
            last_hb = time.time()
            print(f"  ... {int(t_end - time.time())}s left, {len(rows)} rows, cells={len(snap)}", flush=True)

dev.close()
print(f"\nstopped, {len(rows)} rows", flush=True)
wrows = [r for r in rows if "W" in r["os"]]
rest = [r for r in rows if not r["os"]]
print(f"W-held rows: {len(wrows)} rest rows: {len(rest)}", flush=True)
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
    movers.sort(key=lambda x: -x[2])
    print("TOP MOVERS during W-hold (cell, median, max, n):", flush=True)
    for m in movers[:10]:
        print(f"   {m[0]}: median={m[1]} max={m[2]} n={m[3]}", flush=True)
    # also show which banks are present
    present = sorted({k.split('_')[0] for k in base})
    print("banks present:", present, flush=True)
else:
    print("no W or rest rows", flush=True)
