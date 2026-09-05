"""Focused W/S disambiguation (plus A/D re-confirm).

Long self-contained window — no need to watch the console.  12 s baseline
(hands off), then 180 s.  A sample counts for key K only when the OS
confirms EXACTLY {K} is held (solo), so overlapping taps can't contaminate.
Per key we need >= 3 solo samples to call it CONFIDENT; otherwise we
report the top movers with a "LOW CONFIDENCE" flag.

Saves wasd_adc_confirmed.json.
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
                           CMD_RM6X21, CMD_START_ADJUSTING, CMD_SAVE_ADJUSTING)

ADC = 6
KEYS = ["W", "A", "S", "D"]
BASELINE_S = 12.0
WINDOW_S = 180.0
HZ = 15.0
MIN_SOLO = 3

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
        print(f"baseline {len(base)} sensors, detect ~ {GTH:.0f}", flush=True)
        print(f"=== {WINDOW_S:.0f}s: press W and S (and A, D) whenever, one at a "
              f"time, hold 2-3s each ===", flush=True)

        solo: dict[str, list[dict[tuple[int, int], float]]] = {k: [] for k in KEYS}
        t_end = time.time() + WINDOW_S
        cycle = 0.0
        prev: set[str] = set()
        while time.time() < t_end:
            c0 = time.time()
            held = osk.poll()
            adc = read_adc()
            for k in KEYS:
                if held == {k}:
                    devs = {p: v - base[p] for p, v in adc.items() if p in base}
                    solo[k].append(devs)
                    top = sorted(devs.items(), key=lambda kv: -abs(kv[1]))[:2]
                    sig = ", ".join(f"{p}:{d:+.0f}" for p, d in top if abs(d) > GTH * 0.4)
                    if sig:
                        print(f"  t={cycle:6.1f}  solo {k}: {sig}", flush=True)
            if held and held != prev:
                print(f"  t={cycle:6.1f}  OS held: {sorted(held)}", flush=True)
            prev = held
            cycle += 1 / HZ
            el = time.time() - c0
            if el < 1.0 / HZ:
                time.sleep(1.0 / HZ - el)

        print("\n=== RESULTS ===", flush=True)
        out: dict[str, dict] = {}
        for k in KEYS:
            n = len(solo[k])
            print(f"\n  {k}: {n} solo samples", flush=True)
            if n < MIN_SOLO:
                print(f"      LOW CONFIDENCE (<{MIN_SOLO} solo) — top movers so far:", flush=True)
                peak: dict[tuple[int, int], float] = {}
                for devs in solo[k]:
                    for p, d in devs.items():
                        if abs(d) > abs(peak.get(p, 0.0)):
                            peak[p] = d
                top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:5]
                print("      " + ", ".join(f"{p}:{d:+.0f}" for p, d in top), flush=True)
                out[k] = {"n": n, "confident": False,
                          "top": top[:5],
                          "best": (top[0][0][0], top[0][0][1], top[0][1]) if top else None}
                continue
            peak: dict[tuple[int, int], float] = {}
            votes: dict[tuple[int, int], int] = {}
            for devs in solo[k]:
                ranked = sorted(devs.items(), key=lambda kv: -abs(kv[1]))
                top = next(((p, d) for p, d in ranked if abs(d) > GTH), None)
                if top:
                    p, d = top
                    votes[p] = votes.get(p, 0) + 1
                    if abs(d) > abs(peak.get(p, 0.0)):
                        peak[p] = d
            best = max(votes.items(), key=lambda kv: kv[1]) if votes else None
            top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:5]
            print("      top: " + ", ".join(f"{p}:{d:+.0f}" for p, d in top), flush=True)
            print(f"      votes: {votes}", flush=True)
            if best and best[1] >= max(1, MIN_SOLO // 2):
                p, dv = best
                out[k] = {"n": n, "confident": True, "half": p[0],
                          "index": p[1], "dev": peak.get(p, 0.0),
                          "votes": votes, "top": top[:5]}
                print(f"      CONFIRMED: half {p[0]}, idx {p[1]} "
                      f"({best[1]}/{n} votes)", flush=True)
            else:
                out[k] = {"n": n, "confident": False, "votes": votes, "top": top[:5]}
                print("      NOT CONFIRMED (ambiguous votes)", flush=True)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wasd_adc_confirmed.json")
        with open(path, "w") as f:
            json.dump(out, f, indent=2, default=list)
        print(f"\nsaved {path}", flush=True)
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
    print("adjusting session closed — keyboard restored", flush=True)


if __name__ == "__main__":
    main()
