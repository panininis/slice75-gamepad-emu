"""All-interface W capture: log raw vendor banks + kbd (MI_00) + keys (MI_01)
+ joystick reports for 180 s. Press W (only) whenever. After it runs, I
determine which interface carries W's press and whether ANY sensor moves.
"""
import json, os, sys, time, statistics
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,
                           CMD_RM6X21, SUB_READ_MM, SUB_READ_ADC, SUB_READ_RAW,
                           OsKeys)

HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_allif.jsonl")
open(JL, "w").close()
ifs = find_interfaces()
print("interfaces:", {k: v for k, v in ifs.items()}, flush=True)

def open_dev(path):
    d = hid.device(); d.open_path(path); d.set_nonblocking(True)
    return d

devs = {"vend": open_dev(ifs["vendor"]), "kbd": open_dev(ifs["kbd"])}
if "keys" in ifs:
    devs["keys"] = open_dev(ifs["keys"])
vend, kbd = devs["vend"], devs["kbd"]
osk = OsKeys()
vend.write(b"\x00" + build_sync()); time.sleep(0.4)
for d in devs.values():
    t0 = time.time()
    while time.time() - t0 < 0.5:
        d.read(65); time.sleep(0.001)

SEQ = [(SUB_READ_MM, 1), (SUB_READ_MM, 2),
       (SUB_READ_ADC, 1), (SUB_READ_ADC, 2), (SUB_READ_RAW, 1)]

def _plausible(vals):
    ok = sum(1 for v in vals if (1800 < v < 3800) or v == 0)
    return ok >= max(3, len(vals) - 1)

def parse_vend(b):
    if len(b) < 12: return None
    h = b[0]
    if h == 0x5C and b[2] == 0x92 and b[5] in (6, 5):
        c0 = int.from_bytes(b[6:8], "little")
        bank = "A" if c0 < 2950 else "B"
        n = min((len(b) - 6) // 2, 29)
        return bank, {i: int.from_bytes(b[6 + 2*i:8 + 2*i], "little") for i in range(n)}
    if b[1] == 0x0B and h != 0x5C:
        for off in (2, 3):
            if len(b) < off + 14: continue
            vals = [int.from_bytes(b[off + 2*i:off + 2*i + 2], "little") for i in range(6)]
            if _plausible(vals):
                n = min((len(b) - off) // 2, 9)
                cells = {i: int.from_bytes(b[off + 2*i:off + 2*i + 2], "little") for i in range(n)}
                live = sum(1 for v in cells.values() if v > 1800)
                bank = "R2" if live >= 6 else "R1"
                return bank, cells
    return None

print("=== 180s ALL-INTERFACE: press W (only) ===", flush=True)
t_end = time.time() + 180.0
tick = 0
n = 0
last_hb = 0.0
last_w = False
with open(JL, "a") as jl:
    while time.time() < t_end:
        sub, half = SEQ[tick % len(SEQ)]
        try: vend.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
        except OSError: pass
        tick += 1
        snap = {}
        t0 = time.time()
        while time.time() - t0 < 0.120:
            r = vend.read(65)
            if r:
                p = parse_vend(bytes(r))
                if p:
                    bank, cells = p
                    for i, v in cells.items():
                        snap[f"v_{bank}_{i}"] = v
            # also read kbd + vendor-cols raw into same snapshot
            rk = kbd.read(65)
            if rk:
                snap["kbd"] = list(rk[:16])
            if "keys" in devs:
                rk2 = devs["keys"].read(65)
                if rk2:
                    snap["keys"] = list(rk2[:8])
            if time.time() - t0 < 0.120:
                time.sleep(0.0003)
        held = sorted(osk.poll())
        rec = {"t": round(time.time(), 3), "os": held, "raw": snap}
        jl.write(json.dumps(rec) + "\n")
        n += 1
        if "W" in held and not last_w:
            print(f"  >>> W @ {time.strftime('%H:%M:%S')} kbd={snap.get('kbd')} keys={snap.get('keys')}", flush=True)
        last_w = "W" in held
        if time.time() - last_hb >= 30:
            last_hb = time.time()
            print(f"  ... {int(t_end - time.time())}s left {n} rows kbd={snap.get('kbd')}", flush=True)

for d in devs.values():
    try: d.close()
    except Exception: pass
print(f"stopped {n} rows", flush=True)
