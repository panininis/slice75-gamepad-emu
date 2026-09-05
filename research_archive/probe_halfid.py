"""Identify which byte encodes the ADC half, and cell counts per half.

For each half 1,2: send ONE RM6X21(6, half) request, read all replies for
400 ms, and print (in order): byte[0] byte[1] byte[2] len  sub@4 sub@5 and
the first 8 uint16 cell values parsed from [5:] and [6:].  A half press is
NOT needed — we only need to see the two halves' distinct idle signatures
and which flag byte differs.
"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21, SUB_READ_ADC

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"])
dev.set_nonblocking(True)

dev.write(b"\x00" + build_sync()); time.sleep(0.4)

for half in (1, 2):
    print(f"\n=== half {half} ===", flush=True)
    dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_ADC, half)))
    t0 = time.time()
    count = 0
    while time.time() - t0 < 0.4 and count < 12:
        r = dev.read(65)
        if r and r[0] == 0 and r[1] == 0x5C:
            b = bytes(r[1:])
            sub4, sub5 = b[4], b[5]
            cells5 = [int.from_bytes(b[5+2*i:7+2*i], "little") for i in range(8)]
            cells6 = [int.from_bytes(b[6+2*i:8+2*i], "little") for i in range(8)]
            print(f"  [0]={b[0]:02x} [1]={b[1]:02x} [2]={b[2]:02x} len={len(b):2d} "
                  f"sub@4={sub4} sub@5={sub5} c5={cells5[:4]} c6={cells6[:4]}")
            count += 1
        time.sleep(0.001)
    time.sleep(0.1)
print("\ndone")
