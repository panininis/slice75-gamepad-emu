"""Decode the TRUE multi-report structure of an ADC (sub 6) response.

Send ONE RM6X21(6, half) request, then read for 3 s and dump every
report in order.  This reveals:
  * how many 64-byte HID reports one response spans
  * how the data chains (offsets / continuity)
  * whether indices 0..28 of the first report are stable anchors

Also samples the half-2 first-report baseline over 3 s to confirm the
WASD anchors (idx 1,2,3,6) are stable in time.
"""
from __future__ import annotations
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, CMD_START_ADJUSTING, CMD_SAVE_ADJUSTING)

ADC = 6
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


def main():
    dev.write(b"\x00" + build_sync()); drain(0.4)
    # No adjusting session (proven to still answer) — but test both for parity.
    for with_adj in (False, True):
        tag = "ADJ" if with_adj else "NOADJ"
        if with_adj:
            dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ())); drain(0.3)
        print(f"\n########## {tag}: ADC(6, half=2) single request, 3s read ##########")
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (ADC, 2)))
        reps = drain(3.0)
        tot = sum(len(b) for b in reps)
        print(f"{len(reps)} reports, {tot} bytes total")
        for i, b in enumerate(reps):
            print(f"  [{i}] len={len(b)} hdr={b[0]:02x} {b[1]:02x} {b[2]:02x} {b[3]:02x} {b[4]:02x}")
            print(f"       data={b[5:17].hex(' ')} ... {b[-8:].hex(' ')}")
        # ---- stability of first-report anchors over 3 s ----
        print(f"\n{tag}: stability of half-2 first-report idx 0,1,2,3,6 over 3s:")
        series = {}
        t_end = time.time() + 3.0
        while time.time() < t_end:
            dev.write(b"\x00" + build_cmd(CMD_RM6X21, (ADC, 2)))
            rr = drain(0.05)
            first = next((b for b in rr if len(b) > 58 and b[1] == 0x92 and b[4] == ADC), None)
            if first:
                for idx in (0, 1, 2, 3, 6):
                    v = int.from_bytes(first[5 + 2 * idx: 7 + 2 * idx], "little")
                    series.setdefault(idx, []).append(v)
            time.sleep(0.02)
        for idx in (0, 1, 2, 3, 6):
            vs = series.get(idx, [])
            if vs:
                print(f"  idx{idx}: n={len(vs)} min={min(vs)} max={max(vs)} "
                      f"mean={sum(vs)/len(vs):.0f} spread={max(vs)-min(vs)}")
        if with_adj:
            dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ())); drain(0.3)
    dev.close()
    print("\nclosed — keyboard restored (no adjusting session left open)")


if __name__ == "__main__":
    main()
