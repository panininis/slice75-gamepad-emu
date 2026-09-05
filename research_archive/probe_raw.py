"""Raw protocol probe: send the web-driver EXACT RM6X21 packets and dump
every raw 65-byte HID report for 12 s.  This reveals the true on-wire
response layout so the parser can be corrected byte-for-byte.
Also logs the OS key state so we can correlate a keypress with raw frames.
"""
from __future__ import annotations
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import hid  # noqa: E402
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, SUB_READ_MM, SUB_READ_PRESS)

user32 = ctypes.windll.user32
VK = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20}


def os_state() -> str:
    held = sorted(k for k, vk in VK.items() if user32.GetAsyncKeyState(vk) & 0x8000)
    return "".join(held) if held else "."


def main() -> int:
    ifs = find_interfaces()
    if "vendor" not in ifs:
        print("no vendor interface:", list(ifs))
        return 2
    dev = hid.device()
    dev.open_path(ifs["vendor"])
    dev.set_nonblocking(True)

    def drain(n=0.4):
        out = []
        t0 = time.time()
        while time.time() - t0 < n:
            rep = dev.read(65)
            if rep:
                out.append(bytes(rep))
        return out

    # handshake
    dev.write(b"\x00" + build_sync())
    info = drain(0.6)
    print(f"SYNC -> {len(info)} reports")
    for r in info[:3]:
        print("  ", r.hex(" "))

    t_end = time.time() + 12
    cycle = 0
    all_reports: list[tuple[float, bytes, str]] = []
    print("polling RM6X21(2,h) + RM6X21(6,h) for 12s — PRESS A KEY NOW")
    while time.time() < t_end:
        half = (cycle % 2) + 1
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_MM, half)))
        time.sleep(0.005)
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_PRESS, half)))
        for r in drain(0.04):
            all_reports.append((time.time(), r, os_state()))
        cycle += 1
        time.sleep(0.04)
    dev.close()

    print(f"\n=== {len(all_reports)} raw reports ===")
    # unique frames (by payload after report id)
    uniq: dict[bytes, int] = {}
    for _, r, _ in all_reports:
        uniq[r[1:]] = uniq.get(r[1:], 0) + 1
    print(f"unique payloads: {len(uniq)}")
    shown = 0
    for r, n in sorted(uniq.items(), key=lambda kv: -kv[1])[:14]:
        print(f"  x{n:<3} len={len(r):<3} {r.hex(' ')}")
        shown += 1
    # any report while a key was held?
    held = [(t, r, s) for t, r, s in all_reports if s != "."]
    print(f"\nreports while key held: {len(held)}")
    for t, r, s in held[:6]:
        print(f"  os={s} {r.hex(' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
