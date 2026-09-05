"""Solo-sample WASD->ADC correlation — NO timing coordination needed.

10 s baseline (hands off), then a LONG 240 s window.  Samples ~15 Hz:
OS-held WASD set + vendor ADC matrix (RM6X21 sub 6, halves 1+2).

A sample is "solo K" when the OS says exactly {K} of W/A/S/D is held.
Per key: peak |deviation| per sensor across its solo samples.  A key
is only mapped if the top sensor clearly exceeds the noise threshold
(so an OS keypress from a DIFFERENT keyboard — where the Slice75 ADC
doesn't move — is rejected instead of mis-mapped).

Saves wasd_adc_solo.json.  Adjusting session always closed in finally.
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
KEYS = ["W", "A", "S", "D"]
BASELINE_S = 10.0
WINDOW_S = 240.0
TARGET_HZ = 15.0

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)
osk = OsKeys()
print(f"OsKeys.available={osk.available}", flush=True)


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
    res: dict[tuple[int, int], int] = {}
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
        print(f"baseline {BASELINE_S:.0f}s (hands OFF the keyboard)...", flush=True)
        t_end = time.time() + BASELINE_S
        acc: dict[tuple[int, int], list[int]] = {}
        while time.time() < t_end:
            for k, v in read_adc().items():
                acc.setdefault(k, []).append(v)
        base = {k: statistics.mean(vs) for k, vs in acc.items()}
        noise = {k: (statistics.pstdev(vs) if len(vs) > 2 else 5.0)
                 for k, vs in acc.items()}
        thr = {k: max(100.0, 8.0 * noise[k]) for k in base}
        GTH = max(thr.values())
        print(f"baseline {len(base)} sensors, noise-gated detect ~ {GTH:.0f}", flush=True)
        print(f"=== {WINDOW_S:.0f}s window started — press keys whenever you like ===", flush=True)

        solo: dict[str, list[dict[tuple[int, int], float]]] = {k: [] for k in KEYS}
        any_held: dict[str, int] = {k: 0 for k in KEYS}
        solo_events: list[str] = []
        t_end = time.time() + WINDOW_S
        cycle = 0.0
        prev_held: set[str] = set()
        while time.time() < t_end:
            c0 = time.time()
            held = osk.poll()
            adc = read_adc()
            for k in KEYS:
                if k in held:
                    any_held[k] += 1
            for k in KEYS:
                if held == {k}:
                    devs = {p: v - base[p] for p, v in adc.items() if p in base}
                    solo[k].append(devs)
                    top = sorted(devs.items(), key=lambda kv: -abs(kv[1]))[:3]
                    sig = ", ".join(f"{p}:{d:+.0f}" for p, d in top if abs(d) > GTH * 0.4)
                    if sig:
                        solo_events.append(f"  t={cycle:6.1f}  solo {k}: {sig}")
                        print(solo_events[-1], flush=True)
            if held and held != prev_held:
                print(f"  t={cycle:6.1f}  OS held: {sorted(held)}", flush=True)
            prev_held = held
            cycle += 1 / TARGET_HZ
            el = time.time() - c0
            if el < 1.0 / TARGET_HZ:
                time.sleep(1.0 / TARGET_HZ - el)

        print(f"\n=== RESULTS (solo samples: W={len(solo['W'])} "
              f"A={len(solo['A'])} S={len(solo['S'])} D={len(solo['D'])}) ===", flush=True)
        mapping_out: dict[str, dict] = {}
        for k in KEYS:
            if not solo[k]:
                print(f"  {k}: no solo samples (OS saw key-held {any_held[k]} times, "
                      f"but never alone)", flush=True)
                continue
            peak: dict[tuple[int, int], float] = {}
            for devs in solo[k]:
                for p, d in devs.items():
                    if abs(d) > abs(peak.get(p, 0.0)):
                        peak[p] = d
            top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:8]
            print(f"  {k} ({len(solo[k])} solo): "
                  + ", ".join(f"{p}:{d:+.0f}" for p, d in top), flush=True)
            if top and abs(top[0][1]) > GTH:
                mapping_out[k] = {"half": top[0][0][0], "index": top[0][0][1],
                                  "dev": top[0][1]}
        print("\n=== FINAL MAPPING ===", flush=True)
        for k in KEYS:
            m = mapping_out.get(k)
            print(f"  {k}: (half {m['half']}, index {m['index']}) delta {m['dev']:+.0f}"
                  if m else f"  {k}: NOT CONFIRMED", flush=True)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "wasd_adc_solo.json")
        with open(out, "w") as f:
            json.dump(mapping_out, f, indent=2)
        print(f"saved {out}", flush=True)
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
    print("adjusting session closed — keyboard restored", flush=True)


if __name__ == "__main__":
    main()
