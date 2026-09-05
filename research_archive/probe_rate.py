"""Measure the achievable ADC (sub 6) poll rate + per-request latency.

Sequentially issue RM6X21(6, half) as fast as the board will answer,
counting successful frames over ~6 s.  Reports:
  * frames/second per half
  * median + p95 request->response latency
This sets the app's poll_period for a smooth stick.
"""
from __future__ import annotations
import statistics
import sys
import time

sys.path.insert(0, "app")
import hid
from slice_capture import (build_cmd, build_sync, find_interfaces,  # noqa: E402
                           CMD_RM6X21, CMD_START_ADJUSTING, CMD_SAVE_ADJUSTING)

ADC = 6
DUR = 6.0
ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"]); dev.set_nonblocking(True)


def read_frame(sub=ADC, timeout=0.02):
    """Read until one 0x92 report with the given sub; return payload or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rep = dev.read(65)
        if rep:
            b = bytes(rep[1:])
            if len(b) >= 6 and b[1] == 0x92 and b[4] == sub:
                return b
        else:
            time.sleep(0.0005)
    return None


def main():
    dev.write(b"\x00" + build_sync()); time.sleep(0.3)
    # warm-up
    for _ in range(5):
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (ADC, 2)))
        read_frame()
    time.sleep(0.2)

    lat = []
    n1 = n2 = 0
    t_end = time.time() + DUR
    flip = False
    while time.time() < t_end:
        flip = not flip
        half = 2 if flip else 1
        req0 = time.time()
        dev.write(b"\x00" + build_cmd(CMD_RM6X21, (ADC, half)))
        f = read_frame()
        if f is not None:
            lat.append(time.time() - req0)
            if half == 1:
                n1 += 1
            else:
                n2 += 1
    el = time.time() - (t_end - DUR)
    tot = n1 + n2
    print(f"window ~{el:.1f}s: half1={n1} half2={n2} total={tot}")
    print(f"rate: {tot/el:.1f} req/s total  ({n1/el:.1f} per half)")
    if lat:
        lat.sort()
        print(f"latency: median={statistics.median(lat)*1000:.1f}ms "
              f"p95={lat[int(len(lat)*0.95)]*1000:.1f}ms "
              f"max={max(lat)*1000:.1f}ms")
    dev.close()
    print("done (no adjusting session used)")


if __name__ == "__main__":
    main()
