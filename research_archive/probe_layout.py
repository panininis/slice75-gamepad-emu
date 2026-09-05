"""Authoritative WASD -> column from the board's key layout (DEFKEY 0x00).

Robust to header-offset assumptions: dumps the full response and scans
EVERY byte for the WASD scancodes (W=0x11 A=0x1E S=0x1F D=0x20),
reporting the byte offset of each hit.  Also prints the web-driver's
expected row/col interpretation.

No keypresses needed.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces  # noqa: E402

VID, PID = 0x1CA3, 0x0701
WASD_SC = {0x11: "W", 0x1E: "A", 0x1F: "S", 0x20: "D"}


def main() -> int:
    ifs = find_interfaces()  # {'vendor': path, 'kbd': path, 'keys': path}
    dev = hid.device()
    dev.open_path(ifs["vendor"])
    dev.set_nonblocking(True)
    def rd(n=64):
        d = dev.read(n)
        if d is None:
            return None
        return bytes(d) if isinstance(d, (list, tuple)) else d

    dev.write(b"\x00" + build_sync())
    time.sleep(0.2)
    sync = rd()
    print(f"SYNC raw ({len(sync)}B): {sync.hex(' ') if sync else 'NONE'}")

    # send DEFKEY (0x2B = 43), param row 0x00
    dev.write(b"\x00" + build_cmd(0x2B, (0x00,)))
    reports = []
    t_end = time.time() + 0.6
    while time.time() < t_end:
        d = rd()
        if d:
            reports.append(d)
        else:
            time.sleep(0.01)
    print(f"DEFKEY response reports: {len(reports)}")
    for i, r in enumerate(reports):
        print(f"  [{i}] ({len(r)}B) {r.hex(' ')}")

    # pick the largest report as the layout payload
    if reports:
        payload = max(reports, key=len)
        print(f"\n=== scanning payload for WASD scancodes ===")
        for sc, name in WASD_SC.items():
            hits = [j for j in range(len(payload)) if payload[j] == sc]
            print(f"  {name} (0x{sc:02X}) at byte offsets: {hits}")

        # web-driver interpretation: a[1]=row0, a[2..22]=row0 cols,
        # a[23]=row1, a[24..44]=row1 cols, where a[0]=payload[0]
        print("\n=== web-driver row/col interpretation ===")
        row0 = payload[1] if len(payload) > 1 else -1
        row1 = payload[23] if len(payload) > 23 else -1
        found = {}
        for i in range(21):
            if 2 + i < len(payload):
                k = payload[2 + i]
                if k in WASD_SC:
                    found[WASD_SC[k]] = ("row0", row0, i)
        for i in range(21):
            if 24 + i < len(payload):
                k = payload[24 + i]
                if k in WASD_SC:
                    found[WASD_SC[k]] = ("row1", row1, i)
        for k in "WASD":
            print(f"  {k}: {found.get(k)}")

    dev.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc!r}")
        raise
