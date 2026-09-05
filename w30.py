"""30-second ALL-CELL W-capture.

Logs every sensor cell in both banks (A + B) with the raw values, plus the
OS-held WASD set, for 30 s.  Uses the NEW bank classifier (dead-cell
pattern), so each frame is labeled by the bank it actually contains.
Post-hoc: find the cell that drops ~1250 during a W-held run.
"""
import json, sys, time, os
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21, VendorStream

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_w30.jsonl")

import ctypes
user32 = ctypes.windll.user32
VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
def os_keys():
    s = set()
    for k, v in VK.items():
        if user32.GetAsyncKeyState(v) & 0x8000:
            s.add(k)
    return "".join(sorted(s))

def classify(cells):
    """A/B by dead-cell pattern (cells 12 & 22 alive=A, dead=B)."""
    if len(cells) < 29:
        return ""
    c12, c22 = cells[12], cells[22]
    if c12 > 500 and c22 > 500:
        return "A"
    if c12 < 500 or c22 < 500:
        return "B"
    return ""

ifs = find_interfaces()
d = hid.device(); d.open_path(ifs["vendor"]); d.set_nonblocking(True)
time.sleep(0.3)

def read_cells():
    """One RM6X21 request -> parse the response into (bank, cells)."""
    d.write(build_cmd(CMD_RM6X21, (6, 1)))
    deadline = time.time() + 0.008
    while time.time() < deadline:
        try:
            rep = d.read(65)
        except OSError:
            return None, None
        if rep:
            b = bytes(rep)
            if len(b) == 65 and b[0] == 0:
                b = b[1:]
            if len(b) >= 12 and b[0] == 0x5C and b[2] == 0x92:
                cells = [int.from_bytes(b[6 + 2*i:8 + 2*i], "little")
                         for i in range(29)]
                return classify(cells), cells
        time.sleep(0.0003)
    return None, None

# 5 s settle
t0 = time.time()
while time.time() - t0 < 5.0:
    read_cells()

start = time.time()
print("WINDOW 30s start %s — HOLD the physical W key ~5s (don't type)"
      % time.strftime("%H:%M:%S"), flush=True)
n = 0
with open(OUT, "w") as f:
    while time.time() - start < 30.0:
        t = time.time()
        bank, cells = read_cells()
        if bank in ("A", "B") and cells:
            # keep only live cells to keep the file small
            live = {str(i): v for i, v in enumerate(cells) if v > 800}
            f.write(json.dumps({"t": round(t, 4), "os": os_keys(),
                                "bank": bank, "c": live}) + "\n")
            n += 1
        time.sleep(0.0004)
print("stop %s rows=%d" % (time.strftime("%H:%M:%S"), n), flush=True)
d.close()
print("DONE", flush=True)
