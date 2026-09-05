"""Reproduce the vendor driver's EXACT travel-test sequence:
  1) RM6X21(sub=3, half=1)  -- arm/reference
  2) RM6X21(sub=2, half=1)  -- read mm travel, half 1
  3) RM6X21(sub=2, half=2)  -- read mm travel, half 2
  (repeat, alternating)
Dump what sub 2 actually returns on both halves after arming, plus
compare with the sub-6 values we already trust.
"""
import sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"])
time.sleep(0.3)

def send(sub, half):
    d.write(build_cmd(CMD_RM6X21, (sub, half)))

def read_report():
    d.set_report_feature(False) if False else None
    data = None
    for _ in range(40):
        b = d.read(65)
        if b and len(b) >= 10:
            data = b
            break
        time.sleep(0.001)
    return data

# ---- arm like the vendor does ----
send(3, 1)
time.sleep(0.05)

print("=== vendor-exact sequence: sub2 half1 / sub2 half2 (10 pairs) ===")
for i in range(10):
    send(2, 1); r1 = read_report()
    send(2, 2); r2 = read_report()
    def vals(r):
        if not r: return None
        # find sub marker and data
        # try known layouts: data may be at [5..] (sub6 layout) or elsewhere
        body = [int.from_bytes(r[j:j+2], "little") for j in range(5, min(40, len(r)-1), 2)]
        return body[:14]
    v1, v2 = vals(r1), vals(r2)
    print(f"pair{i}: h1={v1}")
    print(f"       h2={v2}")

# Now raw dump of sub2 responses to see real structure
print("=== raw sub2 half1 (6 frames) ===")
for i in range(6):
    send(2, 1); r = read_report()
    if r:
        print("h1 raw:", list(r[:20]))
    time.sleep(0.01)
print("=== raw sub2 half2 (6 frames) ===")
for i in range(6):
    send(2, 2); r = read_report()
    if r:
        print("h2 raw:", list(r[:20]))
    time.sleep(0.01)

d.close()
print("done")
