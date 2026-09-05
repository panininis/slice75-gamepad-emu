"""Event-segmented press capture (no phases, no OS, no digital).

Waits for ANY keypress (ADC deviation), samples until full release,
then expects a ~1.5 s gap before the next event.  Collects up to 4
events.  User presses W, A, S, D in order, releasing fully between each.

For each event, prints the top-deviating sensors (candidate key channels).
Always closes the adjusting session in finally.
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
N_EVENTS = 4

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
                if v:
                    res[(half, i)] = v
    return res


def dev_map(adc: dict, base: dict) -> dict:
    return {p: v - base[p] for p, v in adc.items() if p in base}


def main():
    dev.write(b"\x00" + build_sync()); drain(0.5)
    dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ())); drain(0.3)
    try:
        print("baseline 3s (hands OFF)...")
        acc: dict[tuple[int, int], list[int]] = {}
        for _ in range(30):
            for k, v in read_adc().items():
                acc.setdefault(k, []).append(v)
            time.sleep(0.02)
        base = {k: statistics.mean(vs) for k, vs in acc.items()}
        noise = {k: (statistics.pstdev(vs) if len(vs) > 2 else 5.0)
                 for k, vs in acc.items()}
        thr = max(120.0, max(max(8.0 * noise[k], 0.0) for k in base))
        print(f"baseline {len(base)} sensors, detect ~ {thr:.0f}")

        events: list[dict] = []
        # idle until first press
        t = time.time()
        while not events or len(events) < N_EVENTS:
            adc = read_adc()
            dm = dev_map(adc, base)
            best = max(dm.items(), key=lambda kv: abs(kv[1])) if dm else ((None, 0.0),)
            if best[0] is not None and abs(best[1]) > thr * 0.6:
                # press detected — record until release
                peak: dict[tuple[int, int], float] = {}
                t_press = time.time()
                stable_since = 0.0
                while time.time() - t_press < 6:
                    adc = read_adc()
                    dm = dev_map(adc, base)
                    for p, d in dm.items():
                        if abs(d) > abs(peak.get(p, 0.0)):
                            peak[p] = d
                    still = [p for p, d in dm.items() if abs(d) > thr * 0.35]
                    if not still:
                        if not stable_since:
                            stable_since = time.time()
                        elif time.time() - stable_since > 0.3:
                            break
                    else:
                        stable_since = 0.0
                    time.sleep(0.004)
                top = sorted(peak.items(), key=lambda kv: -abs(kv[1]))[:5]
                events.append({"peak": peak, "top": top,
                               "t": round(time.time() - t, 2)})
                lab = ["W", "A", "S", "D"][len(events) - 1]
                print(f"  EVENT {len(events)} (expect {lab}): " +
                      ", ".join(f"{p}:{d:+.0f}" for p, d in top))
                # gap: wait out release + settle
                time.sleep(1.5)
            else:
                if time.time() - t > 120:
                    print("timeout waiting")
                    break
                time.sleep(0.004)

        print("\n=== RESULT (press order: W, A, S, D) ===")
        chans: dict[str, tuple] = {}
        for i, ev in enumerate(events):
            lab = "WASD"[i]
            top1 = ev["top"][0] if ev["top"] else (None, 0)
            # strongest single sensor (exclude cross-talk < 30% of top)
            primary = top1[0] if top1[0] else None
            chans[lab] = primary
            print(f"  {lab}: {', '.join(f'{p}:{d:+.0f}' for p, d in ev['top'])}")
        print("\nprimary channels:",
              {k: v for k, v in chans.items() if v} or "none")
        # distinctness check
        vals = [v for v in chans.values() if v]
        print("distinct:", len(set(vals)), "of", len(vals))
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
    print("adjusting session closed — keyboard restored to normal")


if __name__ == "__main__":
    main()
