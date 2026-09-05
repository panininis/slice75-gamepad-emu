"""W-only capture: confirm the W sensor position.

OS-confirmed A=(2,1) S=(2,2) D=(2,3) already.  This just nails W.
Hold W (2-3s) whenever ready within 40 s.  Reports which ADC sensor
moves when the OS confirms W is held.
"""
from __future__ import annotations
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import hid  # noqa: E402
from slice_capture import (OsKeys, build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, CMD_START_ADJUSTING,
                           CMD_SAVE_ADJUSTING)

ADC = 6
DUR = 40.0
ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)
osk = OsKeys()
print("W-only capture. Hold W 2-3s. VK fixed.")


def drain(t):
    out = []
    t0 = time.time()
    while time.time() - t0 < t:
        rep = dev.read(65)
        if rep:
            out.append(bytes(rep[1:]))
        else:
            time.sleep(0.0005)
    return out


def read_adc():
    res = {}
    for half in (1, 2):
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (ADC, half)))
        for b in drain(0.012):
            if len(b) < 5 or b[4] != ADC:
                continue
            data = b[5:]
            for i in range(len(data) // 2):
                v = int.from_bytes(data[i * 2:i * 2 + 2], "little")
                res[(half, i)] = v
    return res


def main():
    dev.write(b"\x00" + build_sync()); drain(0.5)
    dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ())); drain(0.3)
    try:
        print("baseline 4s (hands OFF)...")
        acc = {}
        for _ in range(40):
            for k, v in read_adc().items():
                acc.setdefault(k, []).append(v)
            time.sleep(0.02)
        base = {k: statistics.mean(vs) for k, vs in acc.items()}
        thr = {k: max(100.0, 8.0 * (statistics.pstdev(vs) if len(vs) > 2 else 5.0))
               for k, vs in acc.items()}
        GTH = max(thr.values())
        print(f"baseline {len(base)} sensors, detect ~{GTH:.0f}")

        print(f"=== {DUR:.0f}s: HOLD W (2-3s) when ready ===")
        samples = []
        t_end = time.time() + DUR
        last = set()
        while time.time() < t0 if False else time.time() < t_end:
            t0s = time.time()
            held = osk.poll()
            adc = read_adc()
            samples.append((held, adc))
            if held != last:
                print(f"  OS held: {sorted(held) or '-'}")
                last = held
            el = time.time() - t0s
            if el < 0.08:
                time.sleep(0.08 - el)
        w_samples = [adc for held, adc in samples if "W" in held]
        print(f"\\nW OS-confirmed samples: {len(w_samples)}")
        peak = {}
        for adc in w_samples:
            for p, v in adc.items():
                if p in base:
                    d = v - base[p]
                    if abs(d) > abs(peak.get(p, 0.0)):
                        peak[p] = d
        top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:8]
        print("W top movers: " + ", ".join(f"{p}:{d:+.0f}" for p, d in top))
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "w_adc_map.json"), "w") as f:
            json.dump({"top": top}, f, indent=2, default=list)
        print("saved w_adc_map.json")
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
    print("adjusting closed — keyboard restored")


if __name__ == "__main__":
    main()
