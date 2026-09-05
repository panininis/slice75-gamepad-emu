"""Dump the full response headers (bytes 0..7) for half=1 and half=2
responses, so we know exactly what data[5] ("sub") is on the wire.
"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device()
d.open_path(ifs["vendor"])
d.set_nonblocking(True)
time.sleep(0.3)
d.write(b"\x00" + build_sync())
time.sleep(0.4)

def dump(half, n=6):
    got = 0
    while got < n:
        d.write(b"\x00" + build_cmd(CMD_RM6X21, (6, half)))
        for _ in range(30):
            try:
                rep = d.read(65)
            except OSError:
                break
            if not rep:
                time.sleep(0.0004)
                continue
            b = bytes(rep)
            if len(b) == 65 and b[0] == 0:
                b = b[1:]
            if b and len(b) >= 60 and b[2] == 0x92:
                print(f"half={half}: hdr={[hex(x) for x in b[:8]]} "
                      f"len={len(b)} [5]={b[5]} [1]={b[1]}")
                got += 1
                break
            time.sleep(0.0004)
        time.sleep(0.0003)

dump(1)
time.sleep(0.3)
dump(2)
d.close()
