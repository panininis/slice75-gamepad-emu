"""Correlation capture: map W/A/S/D -> ADC positions.

Uses the OS key state (GetAsyncKeyState) as the timing ground truth and the
vendor sub-6 (raw Hall ADC) stream as the analog signal. For each of
W/A/S/D the user holds the key; the probe records which ADC positions
deviate from their resting baseline, by how much, and in which direction.
Always closes the adjusting session on exit.
"""
from __future__ import annotations
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import hid  # noqa: E402
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, CMD_START_ADJUSTING,
                           CMD_SAVE_ADJUSTING)

ADC = 6
user32 = ctypes.windll.user32
VK = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20}


def os_state() -> set[str]:
    return {k for k, vk in VK.items() if user32.GetAsyncKeyState(vk) & 0x8000}


def main() -> int:
    ifs = find_interfaces()
    if "vendor" not in ifs:
        print("no vendor interface", list(ifs)); return 2
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
                time.sleep(0.001)
        return out

    def read_adc() -> dict[tuple[int, int], int]:
        """One round: sub6 for both halves -> {(half, idx): value}."""
        res: dict[tuple[int, int], int] = {}
        for half in (1, 2):
            dev.write(b"\x00" + build_cmd(CMD_RM6X21, (ADC, half)))
            for b in drain(0.03):
                if len(b) < 5 or b[4] != ADC:
                    continue
                data = b[5:]
                for i in range(len(data) // 2):
                    res[(half, i)] = int.from_bytes(
                        data[i * 2:i * 2 + 2], "little")
        return res

    dev.write(b"\x00" + build_sync()); drain(0.5)
    dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ())); drain(0.3)

    # baseline: average ~8 samples while idle
    print("establishing baseline (keep hands off 3s)...")
    base_sum: dict[tuple[int, int], list[int]] = {}
    for _ in range(8):
        for k, v in read_adc().items():
            base_sum.setdefault(k, []).append(v)
        time.sleep(0.05)
    baseline = {k: sum(vs) / len(vs) for k, vs in base_sum.items()}
    print(f"baseline positions: {len(baseline)}  "
          f"min={min(baseline.values()):.0f} max={max(baseline.values()):.0f}")

    results: dict[str, list[tuple[int, float, int]]] = {}
    for key in ("W", "A", "S", "D"):
        print(f"\n>>> NOW HOLD  '{key}'  (waiting up to 15s)...")
        t_wait = time.time()
        while time.time() - t_wait < 15:
            if key in os_state():
                break
            time.sleep(0.01)
        else:
            print(f"   (never saw {key} held)")
            continue
        # while held, record max |deviation| per position + direction
        dev_max: dict[tuple[int, int], int] = {}
        t_hold = time.time()
        print(f"   holding {key}, sampling...")
        while time.time() - t_hold < 4 and key in os_state():
            cur = read_adc()
            for k, v in cur.items():
                if k in baseline:
                    d = v - baseline[k]
                    if abs(d) > abs(dev_max.get(k, 0)):
                        dev_max[k] = d
            time.sleep(0.004)
        top = sorted(dev_max.items(), key=lambda kv: -abs(kv[1]))[:6]
        results[key] = top
        print(f"   {key}: top-deviating positions:")
        for (half, i), d in top:
            print(f"      half{half}[{i:2d}]  {baseline[(half,i)]:7.1f} -> "
                  f"{baseline[(half,i)]+d:7.1f}  (Δ {d:+7.1f})")
        time.sleep(0.5)

    # consistency: which position is shared / distinct per key
    print("\n=== MAPPING CANDIDATES (top-1 per key) ===")
    tops = {k: (results[k][0] if results[k] else None) for k in "WASD"}
    for k, v in tops.items():
        print(f"   {k}: {v}")

    dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
    dev.close()
    print("\nadjusting session closed (keyboard back to normal)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
