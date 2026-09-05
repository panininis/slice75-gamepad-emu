"""Definitive W/A/S/D -> ADC sensor mapping (OS-confirmed).

For ~45 s it samples at ~12 Hz:
   * OS-held key set  (GetAsyncKeyState, correct VK codes)
   * vendor ADC matrix (RM6X21 sub 6, halves 1+2)
Then per key it finds the window where the OS confirms that key is
held and reports which ADC position moved the most -> the mapping.

No strict sequencing: press W, A, S, D whenever you like, one at a
time, 2-3 s each.  Saves wasd_adc_map.json.
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
DUR = 45.0

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)
osk = OsKeys()
print(f"OsKeys.available={osk.available}  (VK codes fixed: W=0x57 A=0x41 S=0x53 D=0x44)")


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


def read_adc() -> dict[tuple[int, int], int]:
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
        # ---- 4 s baseline ----
        print("baseline 4s (hands OFF the keyboard)...")
        acc: dict[tuple[int, int], list[int]] = {}
        for _ in range(40):
            for k, v in read_adc().items():
                acc.setdefault(k, []).append(v)
            time.sleep(0.02)
        base = {k: statistics.mean(vs) for k, vs in acc.items()}
        noise = {k: (statistics.pstdev(vs) if len(vs) > 2 else 5.0)
                 for k, vs in acc.items()}
        thr = {k: max(100.0, 8.0 * noise[k]) for k in base}
        GTH = max(thr.values())
        print(f"baseline sensors: {len(base)}  detect ~ {GTH:.0f}")

        # ---- 45 s sample window ----
        print(f"=== {DUR:.0f}s: press W, A, S, D whenever ready (one at a time, 2-3s each) ===")
        samples: list[tuple[float, set, dict]] = []
        t_end = time.time() + DUR
        last_os: set[str] = set()
        while time.time() < t_end:
            t0 = time.time()
            held = osk.poll()
            adc = read_adc()
            now = t0 - DUR
            samples.append((now, held, adc))
            if held != last_os:
                print(f"  t={now:+6.1f}  OS held: {sorted(held) or '-'}")
                last_os = held
            # keep sample rate ~12 Hz
            el = time.time() - t0
            if el < 0.08:
                time.sleep(0.08 - el)
        print(f"\n{len(samples)} samples collected")

        # ---- per-key correlation ----
        result: dict[str, dict] = {}
        for key in KEYS:
            key_samples = [(s, adc) for s, held, adc in samples if key in held]
            if not key_samples:
                print(f"  {key}: OS never confirmed it as held")
                continue
            peak: dict[tuple[int, int], float] = {}
            for _s, adc in key_samples:
                for p, v in adc.items():
                    if p in base:
                        d = v - base[p]
                        if abs(d) > abs(peak.get(p, 0.0)):
                            peak[p] = d
            top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:6]
            result[key] = {"n_samples": len(key_samples), "top": top}
            print(f"  {key} ({len(key_samples)} OS-confirmed samples): "
                  + ", ".join(f"{p}:{d:+.0f}" for p, d in top))

        print("\n=== FINAL KEY -> ADC POSITION ===")
        mapping_out = {}
        for k in KEYS:
            if k in result and result[k]["top"]:
                p = result[k]["top"][0]
                # only accept if the movement clearly exceeds threshold
                if abs(p[1]) > GTH * 0.5:
                    mapping_out[k] = {"half": p[0], "index": p[1], "dev": p[1]}
                    print(f"  {k}: half{p[0]} index{p[1]}  (delta {p[1]:+.0f})")
                else:
                    print(f"  {k}: movement too small (max {p[1]:+.0f})")
            else:
                print(f"  {k}: NOT CAPTURED")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "wasd_adc_map.json"), "w") as f:
            json.dump(mapping_out, f, indent=2)
        print("saved wasd_adc_map.json")
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
    print("adjusting session closed — keyboard restored")


if __name__ == "__main__":
    main()
