"""Two quick checks:
  A) Verify the FIXED OsKeys detects a synthetic W keypress (SendInput).
  B) Dump one clean ADC (sub 6) frame per half to confirm the exact layout.
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, "app")
import hid
from slice_capture import (  # noqa: E402
    OsKeys, build_cmd, build_sync, find_interfaces,
    CMD_RM6X21, SUB_READ_MM, CMD_START_ADJUSTING, CMD_SAVE_ADJUSTING,
)

user32 = ctypes.windll.user32


def press_w(hold=0.4):
    """Send a synthetic W keydown, hold, then keyup via SendInput."""
    W = 0x57

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint),
                    ("ki", KEYBDINPUT)]

    def send(flag):
        i = _INPUT()
        i.type = 1  # INPUT_KEYBOARD
        i.ki.wVk = W
        i.ki.dwFlags = flag
        n = user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))
        print(f"    SendInput({flag}) -> {n}")

    send(0)          # keydown
    time.sleep(hold)
    send(2)          # keyup


def main() -> int:
    # ---- A) OsKeys fix check ----
    ok = OsKeys()
    print(f"OsKeys.available = {ok.available}")
    print(f"  before: {ok.poll()}")
    press_w(0.5)
    # re-check immediately (key was up; do a fresh hold with a callback)
    # do a hold-then-poll in one shot:
    import threading
    seen = []  # shared list (avoids thread-local |= rebind)
    stop = threading.Event()
    def w():
        while not stop.is_set():
            seen.extend(ok.poll())
            time.sleep(0.01)
    t = threading.Thread(target=w, daemon=True)
    t.start()
    press_w(0.5)
    stop.set()
    t.join()
    print(f"  during synthetic W hold, OsKeys saw: {set(seen) or 'NONE'}")

    # ---- B) clean ADC frame layout ----
    ifs = find_interfaces()
    dev = hid.device()
    dev.open_path(ifs["vendor"])
    dev.set_nonblocking(True)
    def rd(n=65):
        d = dev.read(n)
        if not d:
            return None
        d = bytes(d) if isinstance(d, (list, tuple)) else bytes(d)
        # strip leading report ID 0x00 if present
        if len(d) > 64 and d[0] == 0:
            d = d[1:]
        return d[:64]
    dev.write(b"\x00" + build_sync())
    time.sleep(0.2)
    sync = rd()
    print(f"\nSYNC: {sync[:16].hex(' ') if sync else 'NONE'}")
    dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ()))
    time.sleep(0.3)
    for half in (1, 2):
        got = False
        for _ in range(15):
            dev.write(b"\x00" + build_cmd(CMD_RM6X21, (6, half)))
            time.sleep(0.04)
            r = rd()
            if r and len(r) > 58 and r[1] == 0x92:
                vals = [int.from_bytes(r[5 + 2 * i: 7 + 2 * i], "little")
                        for i in range(29)]
                nz = [(i, v) for i, v in enumerate(vals) if v]
                print(f"\nADC half={half}: {len(r)}B hdr={r[0]:02x} sub={r[4]}")
                print(f"  {len(nz)} nonzero: {nz[:14]}")
                got = True
                break
        if not got:
            print(f"\nADC half={half}: no frame after 15 tries")
    dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ()))
    time.sleep(0.2)
    dev.close()
    print("\nadjusting closed — keyboard restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
