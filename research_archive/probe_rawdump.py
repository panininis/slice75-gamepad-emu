"""Raw frame dump: what does the ADC response ACTUALLY look like on the wire?"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21, SUB_READ_ADC

ifs = find_interfaces()
dev = hid.device()
dev.open_path(ifs["vendor"])
dev.set_nonblocking(True)

dev.write(b"\x00" + build_sync()); time.sleep(0.4)
# drain
t0 = time.time()
while time.time() - t0 < 0.5:
    dev.read(65); time.sleep(0.001)

print("sending RM6X21(6, half=1)...", flush=True)
dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_ADC, 1)))
t0 = time.time()
n = 0
while time.time() - t0 < 1.0 and n < 8:
    r = dev.read(65)
    if r:
        n += 1
        b = bytes(r)
        print(f"[half1] len={len(b)} b[1]={b[1]:02x} hex: {b[:10].hex(' ')}", flush=True)
    time.sleep(0.001)
print(f"got {n} reports", flush=True)
time.sleep(0.1)
print("\nsending RM6X21(6, half=2)...", flush=True)
dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_ADC, 2)))
t0 = time.time()
n = 0
while time.time() - t0 < 1.0 and n < 8:
    r = dev.read(65)
    if r:
        n += 1
        b = bytes(r)
        print(f"[half2] len={len(b)} b[1]={b[1]:02x} hex: {b[:10].hex(' ')}", flush=True)
    time.sleep(0.001)
print(f"got {n} reports", flush=True)
dev.close()
