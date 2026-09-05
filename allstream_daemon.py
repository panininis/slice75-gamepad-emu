"""Persistent ALL-STREAM logger (standalone device, no shared poll thread).

Directly drives the vendor endpoint (no VendorStream thread), requesting
each sub (6, 5, 11) x half (1, 2) in turn with an 80 ms burst drain (sub 11
answers in a delayed burst), then logs ALL cells + the OS-held WASD set to
wasd_allstream.jsonl every ~0.6 s.  Runs until killed.  The user presses W
(only) at their convenience and messages after; I then read the file and
find the (sub, half, idx) cell that dropped.
"""
import json, os, sys, time, statistics
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,
                           CMD_RM6X21, SUB_READ_ADC, SUB_READ_ADC_ALT,
                           SUB_READ_RAW)
from slice_capture import OsKeys

HERE = os.path.dirname(os.path.abspath(__file__))
JL = os.path.join(HERE, "wasd_allstream.jsonl")
SUBS = [SUB_READ_ADC, SUB_READ_ADC_ALT, SUB_READ_RAW]
HALFS = [1, 2]
DRAIN = 0.08
N_CELLS = 29

open(JL, "w").close()
ifs = find_interfaces()
if "vendor" not in ifs:
    print("vendor not found"); sys.exit(2)
dev = hid.device()
dev.open_path(ifs["vendor"])
dev.set_nonblocking(True)
osk = OsKeys()

def ask(sub, half):
    """One request + burst drain -> (bank, {idx: value}) or None.

    The board ignores/unreliably honors the half param and alternates
    between two sensor banks at its own pace.  The bank is detected from
    the first cell: bank A c0 ~2916-2918, bank B c0 ~2978-2982."""
    try:
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (sub, half)))
    except OSError:
        return None
    t0 = time.time()
    while time.time() - t0 < DRAIN:
        r = dev.read(65)
        if r:
            b = bytes(r)
            if len(b) >= 8 and b[0] == 0x5C and b[2] == 0x92 and b[5] == sub:
                got = {i: int.from_bytes(b[6 + 2*i:8 + 2*i], "little")
                       for i in range(min((len(b) - 6) // 2, N_CELLS))}
                if 0 in got:
                    bank = "A" if got[0] < 2950 else "B"
                    return bank, got
        time.sleep(0.0003)
    return None

# handshake
dev.write(b"\x00" + build_sync())
time.sleep(0.5)
t0 = time.time()
while time.time() - t0 < 0.6:
    dev.read(65); time.sleep(0.001)

print(f"ALL-STREAM DAEMON live -> {JL}", flush=True)
print("press W (only) when ready; daemon runs until killed", flush=True)

n = 0
last_hb = time.time()
last_w = False
with open(JL, "a") as jl:
    while True:
        t0 = time.time()
        snap = {}
        # several sub-6 requests to catch BOTH banks (board alternates)
        for k in range(6):
            res = ask(SUB_READ_ADC, 1)
            if res:
                bank, cells = res
                for i, v in cells.items():
                    snap[f"s6_{bank}_{i}"] = v
        for sub in (SUB_READ_ADC_ALT, SUB_READ_RAW):
            res = ask(sub, 1)
            if res:
                bank, cells = res
                for i, v in cells.items():
                    snap[f"s{sub}_{bank}_{i}"] = v
        held = sorted(osk.poll())
        rec = {"t": round(time.time(), 3), "os": held, "raw": snap}
        jl.write(json.dumps(rec) + "\n")
        n += 1
        if "W" in held and not last_w:
            print(f"  >>> W pressed at {time.strftime('%H:%M:%S')} "
                  f"(snapshot {n})", flush=True)
        last_w = "W" in held
        now = time.time()
        if now - last_hb >= 60.0:
            last_hb = now
            print(f"  ... {n} rows @ {time.strftime('%H:%M:%S')} "
                  f"cells={len(snap)} os={held}", flush=True)
        # pace to ~0.6 s per snapshot
        dt = time.time() - t0
        if dt < 0.6:
            time.sleep(0.6 - dt)
