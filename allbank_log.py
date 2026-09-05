"""Persistent ALL-bank Hall logger (standalone device).

Runs until killed. Logs every sensor cell in every bank the board
streams (A, B, + rare R/0x4A) with a timestamp and the OS-held WASD set.

Banks are identified by frame head + cell signature (NOT by request,
since the board ignores the half param and cycles banks):
  head 0x5C, sig ~[2918,2886,2956,...]  -> A  (top rows: number/QWERTY)
  head 0x5C, sig ~[2981,2929,2937,3025] -> B  (home row: A,S,D,F,....)
  other heads (0x4A/0x5D/0x5E/0x49...)  -> R  (extra streams)

Usage: just run it. Press W (and A/S/D for reference) on the Slice.
Stop it (or tell the agent) when done; the JSONL is analyzed after.
"""
import json, sys, time, os
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, find_interfaces, CMD_RM6X21

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_allbank.jsonl")

# ---- OS key state (VK codes) ----
import ctypes
user32 = ctypes.windll.user32
VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
def os_keys():
    s = set()
    for k, v in VK.items():
        if user32.GetAsyncKeyState(v) & 0x8000:
            s.add(k)
    return "".join(sorted(s))

def parse(b):
    """Return (bank, [cells]) from a response frame, or (None, None)."""
    if len(b) < 12:
        return None, None
    h = b[0]
    cells = []
    for j in range(6, min(len(b) - 1, 6 + 58), 2):
        cells.append(int.from_bytes(b[j:j + 2], "little"))
    # bank A vs B: A c0~2918 c1~2886; B c0~2981 c1~2929
    if h == 0x5C and len(cells) >= 4:
        if abs(cells[1] - 2886) < 40 and abs(cells[0] - 2918) < 40:
            return "A", cells
        if abs(cells[1] - 2929) < 40 and abs(cells[0] - 2981) < 40:
            return "B", cells
    return "R" if h not in (0x00, 0x5C) else "X", cells

def main():
    ifs = find_interfaces()
    d = hid.device(); d.open_path(ifs["vendor"]); time.sleep(0.3)
    # warm-up: settle baselines
    for _ in range(60):
        d.write(build_cmd(CMD_RM6X21, (6, 1)))
        for _ in range(30):
            if d.read(65): break
        time.sleep(0.0005)

    print("allbank logger running -> %s  (Ctrl-C to stop)" % OUT, flush=True)
    n = 0
    while True:
        t = time.time()
        d.write(build_cmd(CMD_RM6X21, (6, 1)))
        bank, cells = None, None
        for _ in range(40):
            b = d.read(65)
            if b and len(b) >= 12:
                bank, cells = parse(b)
                break
            time.sleep(0.0004)
        if bank and cells and bank in ("A", "B", "R"):
            row = {"t": round(t, 3), "os": os_keys(), "bank": bank}
            live = {str(i): v for i, v in enumerate(cells) if v > 800}
            if live:
                row["c"] = live
                with open(OUT, "a") as f:
                    f.write(json.dumps(row) + "\n")
                n += 1
        # target ~150 Hz
        dt = time.time() - t
        if dt < 0.006:
            time.sleep(0.006 - dt)
        if n % 500 == 0 and n > 0:
            print("rows=%d" % n, flush=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
    except Exception as e:
        import traceback; traceback.print_exc()
