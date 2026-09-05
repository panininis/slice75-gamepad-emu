"""Self-synchronizing key->ADC mapping (no OS / no digital needed).

For each of W, A, S, D the probe:
   1. prints "HOLD <key>" and waits (up to 25 s) until some ADC sensor
      deviates beyond a noise-scaled threshold  -> press detected
   2. samples while held, recording the peak deviation of every sensor
   3. waits until the pressed sensor returns near baseline  -> release

The position with the largest deviation is that key's ADC channel.
Threshold is auto-derived from the per-sensor baseline noise, so it adapts.
The adjusting session is ALWAYS closed in finally (keyboard restored).
"""
from __future__ import annotations
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import hid  # noqa: E402
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, CMD_START_ADJUSTING,
                           CMD_SAVE_ADJUSTING)

ADC = 6
KEYS = ["W", "A", "S", "D"]

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)


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
                if v:                      # skip unused zero slots
                    res[(half, i)] = v
    return res


def main():
    dev.write(b"\x00" + build_sync()); drain(0.5)
    dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ())); drain(0.3)
    try:
        # ---- baseline + noise (30 samples) ----
        print("baseline 3s (hands OFF the keyboard)...")
        acc: dict[tuple[int, int], list[int]] = {}
        for _ in range(30):
            for k, v in read_adc().items():
                acc.setdefault(k, []).append(v)
            time.sleep(0.02)
        base = {k: statistics.mean(vs) for k, vs in acc.items()}
        noise = {k: (statistics.pstdev(vs) if len(vs) > 2 else 5.0)
                 for k, vs in acc.items()}
        thr = {k: max(120.0, 8.0 * noise[k]) for k in base}
        print(f"baseline sensors: {len(base)}")
        # global detection threshold = max of per-sensor thresholds
        GTH = max(thr.values())
        print(f"detect threshold ~ {GTH:.0f} counts")
        print("\n>>> GET READY — holding sequence starts in 10 s ...")
        for i in range(10, 0, -1):
            print(f"    {i}", end="\r", flush=True)
            time.sleep(1.0)
        print()

        mapping: dict[str, tuple] = {}
        for key in KEYS:
            print(f"\n>>> HOLD  '{key}'  now... (waiting up to 25s)")
            pressed = None
            t = time.time()
            while time.time() - t < 25:
                adc = read_adc()
                best = None; bestd = 0
                for p, v in adc.items():
                    if p in base:
                        d = abs(v - base[p])
                        if d > GTH * 0.6 and d > bestd:   # pre-threshold to catch it
                            best, bestd = p, d
                if best is not None:
                    pressed = best
                    break
                time.sleep(0.004)
            if pressed is None:
                print(f"   {key}: no press detected within 25 s")
                continue
            # sample while held (up to 5 s), until release
            peak: dict[tuple[int, int], float] = {pressed: 0.0}
            signed = 0.0
            t = time.time()
            released = False
            while time.time() - t < 5:
                adc = read_adc()
                if pressed in adc:
                    d = adc[pressed] - base[pressed]
                    signed = d
                    if abs(d) > abs(peak[pressed]):
                        peak[pressed] = d
                    # record top-3 movers this instant
                    ranked = sorted(((p, v - base[p]) for p, v in adc.items()
                                     if p in base), key=lambda kv: -abs(kv[1]))
                    for p, dd in ranked[:3]:
                        peak[p] = dd if abs(dd) > abs(peak.get(p, 0)) else peak.get(p, 0)
                else:
                    continue
                # release when the pressed sensor is back near baseline
                if pressed in adc and abs(adc[pressed] - base[pressed]) < GTH * 0.35:
                    released = True
                    break
                time.sleep(0.004)
            top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:4]
            mapping[key] = (pressed, signed, released)
            print(f"   {key}: primary {pressed}  peak {signed:+.0f}  "
                  f"released={released}")
            print(f"        top movers: " +
                  ", ".join(f"{p}:{d:+.0f}" for p, d in top))
            time.sleep(0.6)

        print("\n=== KEY -> ADC POSITION (primary) ===")
        for k in KEYS:
            if k in mapping:
                p, s, rel = mapping[k]
                print(f"   {k}: half{p[0]} index{p[1]}  (peak {s:+.0f})")
            else:
                print(f"   {k}: NOT CAPTURED")
        # consistency check
        primaries = {k: mapping[k][0] for k in mapping}
        uniq = set(primaries.values())
        print(f"\ndistinct primary positions: {len(uniq)} of {len(primaries)}")
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
    print("adjusting session closed — keyboard restored to normal")


if __name__ == "__main__":
    main()
