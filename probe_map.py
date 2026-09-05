"""DEFINITIVE key->analog mapping.

Runs 90 s.  Opens BOTH the digital key interface (MI_01) and the vendor
ADC stream (sub-6, adjusting session active).  For the whole window it
samples:
   * digital key set (which key is physically held, by HID usage)
   * the vendor ADC matrix
Whenever the digital key set changes, it records which ADC positions have
moved > threshold from the resting baseline for THAT held key.

Result: for each of W/A/S/D, the exact ADC position(s) that move.
The adjusting session is ALWAYS closed on exit (finally) so the keyboard
returns to normal (no lingering sluggishness).

INSTRUCTIONS: during the 90 s, press and hold each of W, A, S, D for about
2-3 seconds, one at a time, with a pause between.  Order doesn't matter.
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
                           CMD_SAVE_ADJUSTING, DigitalKeys, _USAGE2K)

ADC = 6
user32 = ctypes.windll.user32
VK = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20}

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)
dk = DigitalKeys(ifs["keys"]) if "keys" in ifs else None
if dk:
    dk.open()


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
                res[(half, i)] = int.from_bytes(data[i * 2:i * 2 + 2], "little")
    return res


def os_state():
    return {k for k, vk in VK.items() if user32.GetAsyncKeyState(vk) & 0x8000}


def main():
    dev.write(b"\x00" + build_sync()); drain(0.5)
    dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ())); drain(0.3)
    try:
        # baseline
        print("baseline 2s (hands off)...")
        bs = {}
        for _ in range(10):
            for k, v in read_adc().items():
                bs.setdefault(k, []).append(v)
            time.sleep(0.05)
        base = {k: sum(v) / len(v) for k, v in bs.items()}
        print(f"baseline {len(base)} positions")

        # main loop
        print("=== 75s: hold W, A, S, D one at a time (2-3s each) ===")
        t_end = time.time() + 75
        last_dig = frozenset()
        per_key_dev: dict[str, dict[tuple[int, int], int]] = {}
        any_dig = False
        any_os = False
        while time.time() < t_end:
            dig = dk.poll() if (dk and dk.opened) else set()
            osst = os_state()
            if dig:
                any_dig = True
            if osst:
                any_os = True
            adc = read_adc()
            # current single held key (prefer digital; fall back to OS)
            held = set(dig) & {"W", "A", "S", "D"}
            if not held:
                held = osst & {"W", "A", "S", "D"}
            if held and len(held) == 1:
                k = next(iter(held))
                slot = per_key_dev.setdefault(k, {})
                for p, v in adc.items():
                    if p in base:
                        d = v - base[p]
                        if abs(d) > abs(slot.get(p, 0)):
                            slot[p] = d
            time.sleep(0.004)

        print(f"\n=== digital(MI_01) ever active: {any_dig}   OS ever active: {any_os} ===")
        for k in "WASD":
            dev_map = per_key_dev.get(k)
            if not dev_map:
                print(f"  {k}: (no movement captured)")
                continue
            top = sorted(dev_map.items(), key=lambda kv: -abs(kv[1]))[:5]
            line = ", ".join(f"half{h}[{i}]:{d:+.0f}" for (h, i), d in top)
            print(f"  {k}: {line}")
    finally:
        dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
        dev.close()
        if dk:
            dk.close()
    print("adjusting session closed — keyboard restored to normal")


if __name__ == "__main__":
    main()
