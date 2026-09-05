"""Run the vendor driver's full init handshake, then test RM6X21.

Sequence (from driver_main.js, App-mode init):
  SYNC (0x01) -> info
  CMDPack(1)      protocol version
  CMDPack(38)     keyboard name
  CMDPack(37)     precision
  CMDPack(112,4)  config id
  CMDPack(118)    axis list
  CMDPack(80,7)   rate of return
  CMDPack(39,100) color
  CMDPack(40,100) default axis
  CMDPack(33)     win mode
  CMDPack(34)     mac mode
then RM6X21(6,1) stream test.
"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); d.set_nonblocking(True)
time.sleep(0.3)

def req(payload):
    d.write(payload)

def read_frames(secs=0.3):
    out = []
    t0 = time.time()
    while time.time() - t0 < secs:
        rep = d.read(65)
        if rep:
            b = bytes(rep)
            if len(b) == 65 and b[0] == 0:
                b = b[1:]
            if b:
                out.append(b)
        else:
            time.sleep(0.001)
    return out

def cmdpack(order, arg=None):
    return b"\x00" + build_cmd(0, (order,) if arg is None else (order, arg))

print("SYNC...", flush=True)
req(b"\x00" + build_sync())
for r in read_frames(0.4):
    print("  0x%02X: %s" % (r[2], r[:16].hex()), flush=True)

steps = [(1, None), (38, None), (37, None), (112, 4), (118, None),
         (80, 7), (39, 100), (40, 100), (33, None), (34, None)]
for order, arg in steps:
    req(cmdpack(order, arg))
    rs = read_frames(0.25)
    cls = [r[2] for r in rs]
    print(f"CMD({order}{',' + str(arg) if arg is not None else ''}) -> {cls} "
          f"({len(rs)} frames)", flush=True)

time.sleep(0.5)
# RM6X21 test
ok = 0
for _ in range(500):
    d.write(build_cmd(CMD_RM6X21, (6, 1)))
    rep = d.read(65)
    if rep:
        b = bytes(rep)
        if len(b) == 65 and b[0] == 0:
            b = b[1:]
        if b and b[0] == 0x5C and len(b) >= 12 and b[2] == 0x92:
            ok += 1
    else:
        time.sleep(0.001)
print("RM6X21 after full init:", ok, "frames", flush=True)
d.close()
