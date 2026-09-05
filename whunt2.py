"""Definitive W-hunter: sample ALL Hall bank streams (sub6 A/B + sub11
0x5D + 0x49) continuously, log snapshots + OS keys for 90 s.

The board has 4 bank streams:
  sub 6/5, head 0x5C: bank A (c0 ~2917) / bank B (c0 ~2980), 29 cells
  sub 11, head 0x5D: bank 1, ~8 cells (data at bytes [2..19])
  sub 11, head 0x49: bank 2, ~6 cells (data at bytes [2..13])
A single sub-6 request makes the board stream BOTH 0x5C banks at ~2 ms each,
so we just keep requesting and parse every frame by its head.

Find: which cell drops ~1200 while W is OS-held.
"""
import json, os, sys, time, statistics
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,
                           CMD_RM6X21, SUB_READ_ADC, SUB_READ_RAW, OsKeys)

JL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_whunt2.jsonl")
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

def _plausible(vals):
    ok = sum(1 for v in vals if (1800 < v < 3800) or v == 0)
    return ok >= max(3, len(vals) - 1)

def parse(b):
    """Return (bank, {idx: val}) for any known frame, else None.

    Frames:
      head 0x5C, class 0x92 @ b[2], sub @ b[5] in (6,5): sub-6 bank
        A/B; data = 29 x uint16 LE from b[6].  Bank by c0 signature.
      head 0x5D/0x5E: sub-11 bank R1; data = uint16 LE, offset b[2] or
        b[3] (auto-detected by plausibility).
      head 0x49: sub-11 bank R2; same auto-offset.
    """
    if len(b) < 12:
        return None
    h = b[0]
    if h == 0x5C and b[2] == 0x92 and b[5] in (6, 5):
        c0 = int.from_bytes(b[6:8], "little")
        bank = "A" if c0 < 2950 else "B"
        n = min((len(b) - 6) // 2, 29)
        return bank, {i: int.from_bytes(b[6 + 2*i:8 + 2*i], "little") for i in range(n)}
    # sub-11 banks: head byte VARIES with data (0x5D/0x5F/0x49...); the
    # reliable signature is b[1] == 0x0b and a run of plausible uint16 from
    # b[2].  Two banks arrive per request (R1 then R2).
    if b[1] == 0x0B and b[0] != 0x5C:
        vals = [int.from_bytes(b[2 + 2*i:4 + 2*i], "little") for i in range(6)]
        if _plausible(vals):
            # bank id: R1 has ~4 live cells then zeros; R2 ~6-7 live
            n = min((len(b) - 2) // 2, 9)
            cells = {i: int.from_bytes(b[2 + 2*i:4 + 2*i], "little") for i in range(n)}
            live = sum(1 for v in cells.values() if v > 1800)
            bank = "R2" if live >= 6 else "R1"   # R1 has 5 live, R2 has 7
            return bank, cells
    return None

# continuous request pump
last6 = 0.0
last11 = 0.0
t_end = time.time() + 90.0
rows = []
last_w = False
print("=== 90s: HOLD W ~5s (W-hunter, all 4 bank streams) ===", flush=True)
last_hb = 0.0
with open(JL, "a") as jl:
    while time.time() < t_end:
        now = time.time()
        if now - last6 > 0.020:
            try: dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_ADC, 1)))
            except OSError: pass
            last6 = now
        if now - last11 > 0.050:
            try: dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_RAW, 1)))
            except OSError: pass
            last11 = now
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
                time.sleep(0.0005)
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
else:
    print("no W or rest rows", flush=True)
