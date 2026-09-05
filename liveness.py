"""Board liveness monitor: log every byte from the vendor endpoint for 60 s.
Tracks: (a) any 0x92 ADC frames, (b) any 0x81 SYNC frames, (c) total reads.
Also sends a SYNC heartbeat every 2 s (the way the web driver keeps the
board awake) to test whether the board is in active-sleep.
"""
import sys, time, json, os
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,
                           CMD_RM6X21)

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); d.set_nonblocking(True)
time.sleep(0.3)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "liveness.jsonl")
n92 = n81 = nother = ntot = 0
hb = 0.0
t0 = time.time()
with open(out, "w") as f:
    while time.time() - t0 < 90.0:
        # heartbeat: SYNC every 2 s
        if time.time() - hb >= 2.0:
            try:
                d.write(b"\x00" + build_sync())
                hb = time.time()
            except OSError:
                pass
        # ADC poll
        try:
            d.write(build_cmd(CMD_RM6X21, (6, 1)))
        except OSError:
            pass
        # drain reads
        for _ in range(20):
            try:
                rep = d.read(65)
            except OSError:
                break
            if rep:
                b = bytes(rep)
                if len(b) == 65 and b[0] == 0:
                    b = b[1:]
                if b:
                    ntot += 1
                    cls = b[2] if len(b) > 2 else -1
                    if cls == 0x92:
                        n92 += 1
                        f.write(json.dumps({"t": round(time.time()-t0, 3),
                                            "k": "adc", "raw": list(b[:20])}) + "\n")
                    elif cls == 0x81:
                        n81 += 1
                    else:
                        nother += 1
            else:
                time.sleep(0.0005)
        time.sleep(0.0005)
    print(f"90s done: total={ntot} adc(0x92)={n92} sync(0x81)={n81} other={nother}")
d.close()
print("DONE")
