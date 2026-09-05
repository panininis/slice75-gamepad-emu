"""Ground-truth capture: records EXACTLY what the keyboard emits when you
press keys.  Run it, then press the keys as instructed.

It records (with timestamps) for up to 90 s:
  * MI00 analog block (6 bytes, 0..255)  — the HID analog channels
  * MI02 vendor stream raw frames        — the proprietary travel stream
  * OS key state (GetAsyncKeyState)      — what Windows sees
  * MI01 digital reports                 — should be silent (known)

At the end it prints which sources moved, so we can see which one carries
the analog travel and where WASD live.
"""
from __future__ import annotations
import ctypes
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from slice_capture import HidAnalog, VendorStream, find_interfaces  # noqa: E402

user32 = ctypes.windll.user32
VK = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20}
EVENTS: list[dict] = []
t_end = time.time() + 90
_last_analog = None
_last_ven: tuple | None = None


def os_state() -> str:
    held = sorted(k for k, vk in VK.items() if user32.GetAsyncKeyState(vk) & 0x8000)
    return "".join(held) if held else "."


def main() -> int:
    ifs = find_interfaces()
    print(f"interfaces: {list(ifs)}")
    hid = HidAnalog(ifs["kbd"]) if "kbd" in ifs else None
    ven = VendorStream(ifs["vendor"]) if "vendor" in ifs else None
    hid.open() if hid else None
    if ven:
        ven.open()
    print("=== CAPTURING for 90s — press keys now: ===")
    print("1) HOLD W for 3s   2) release   3) HOLD A for 3s   4) release")
    print("5) HOLD S for 3s   6) release   7) HOLD D for 3s   8) release")
    print("9) press W and D TOGETHER for 2s")
    global _last_analog, _last_ven
    while time.time() < t_end:
        if hid:
            hid.poll()
            a = tuple(hid.latest)
            if a != _last_analog:
                _last_analog = a
                EVENTS.append({"t": round(time.time(), 3), "src": "MI00-analog",
                               "data": list(a), "os": os_state()})
        if ven:
            with ven._lock:
                v = tuple(sorted(ven.mm.items()))
            if v and v != _last_ven:
                _last_ven = v
                EVENTS.append({"t": round(time.time(), 3), "src": "vendor-matrix",
                               "data": v, "os": os_state(),
                               "frames": ven.frame_count})
        time.sleep(0.001)
    # vendor frames were consumed by the reader; re-open once more for a
    # final matrix snapshot
    if ven:
        with ven._lock:
            snap = dict(ven.mm)
    else:
        snap = {}
    if hid:
        hid.close()
    if ven:
        ven.close()

    # ---------------- analysis ----------------
    if not EVENTS:
        print("\n*** NO MI00 analog changes were seen at all. ***")
        print(f"vendor matrix snapshot: {snap}")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "capture_wasd.json"), "w") as f:
            json.dump({"events": EVENTS, "vendor": snap}, f, indent=1)
        return 1

    print(f"\n=== {len(EVENTS)} analog changes captured ===")
    # bucket by which channels moved
    ch_moves: dict[int, int] = {}
    os_seen: set[str] = set()
    for e in EVENTS:
        for i, v in enumerate(e["data"]):
            if v > 0:
                ch_moves[i] = ch_moves.get(i, 0) + 1
        os_seen.update(e["os"].replace(".", ""))
    print("channels that went non-zero:",
          {i: f"n={n}" for i, n in sorted(ch_moves.items())})
    ven_events = [e for e in EVENTS if e["src"] == "vendor-matrix"]
    hid_events = [e for e in EVENTS if e["src"] == "MI00-analog"]
    print(f"vendor-matrix events: {len(ven_events)}   MI00 events: {len(hid_events)}")
    if ven_events:
        allpos = set()
        for e in ven_events:
            for pos, mm in e["data"]:
                if mm > 0:
                    allpos.add(pos)
        print("vendor positions that went non-zero:", sorted(allpos))
    print("OS saw these keys held:", sorted(os_seen) or "NONE")
    print("\n--- first 15 events ---")
    for e in EVENTS[:15]:
        d = e["data"]
        if isinstance(d, list) and len(d) == 6:
            d = f"analog{d}"
        print(f"  t={e['t']:<10} {e['src']:<14} {str(d)[:90]} os={e['os']}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "capture_wasd.json")
    with open(out, "w") as f:
        json.dump({"events": EVENTS, "vendor_snapshot": snap}, f, indent=1)
    print(f"\nsaved full capture -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
