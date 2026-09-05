"""Test whether the `half` param selects a bank or is ignored.

Phase 1: send half=1 x 40, classify each response (A or B).
Phase 2: send half=2 x 40, classify each response (A or B).
Phase 3: send half=1,2 alternating x 40 (like the vendor driver), classify.

If half=1 -> mostly A and half=2 -> mostly B, the param WORKS.
If both mix, the param is IGNORED and the stream self-cycles A,B,A,B...
"""
import sys, time, collections
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21

src = open("allbank_log.py").read()
ns = {"__file__": "allbank_log.py", "__name__": "notmain"}
exec(compile(src, "allbank_log.py", "exec"), ns)
parse = ns["parse"]

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); time.sleep(0.3)

def one(half):
    d.write(build_cmd(CMD_RM6X21, (6, half)))
    for _ in range(40):
        b = d.read(65)
        if b and len(b) >= 12:
            bank, cells = parse(b)
            return bank, cells
        time.sleep(0.0004)
    return None, None

def run(label, halves, n=40):
    cnt = collections.Counter()
    seq = []
    for i in range(n):
        bank, _ = one(halves[i % len(halves)])
        if bank in ("A", "B"):
            cnt[bank] += 1
            seq.append(bank)
        time.sleep(0.0004)
    print(f"{label}: {dict(cnt)}  seq='{''.join(seq)}'")

print("settling..."); time.sleep(0.5)
run("half=1 x40", [1])
time.sleep(0.3)
run("half=2 x40", [2])
time.sleep(0.3)
run("alternating 1,2 x40", [1, 2])
d.close()
print("done")
