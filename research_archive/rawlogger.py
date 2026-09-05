"""Persistent raw logger (single long-lived process — NEVER opens/closes
the device repeatedly; that flapping may be what confuses the firmware).

Opens the vendor endpoint ONCE and keeps it open forever. Logs:
  * every raw RM6X21 (0x92) frame: head byte + all 29 raw cells
  * every SYNC (0x81) frame
  * OS WASD held state (GetAsyncKeyState, global)
  * a heartbeat (SYNC) every 2 s to keep the link alive

The board sleeps after ~60 s of inactivity (active sleep, wakes on key
press). So: whenever the user presses any key, the ADC stream comes back
for the next ~60 s and this logger captures every cell.

Output: wasd_rawall.jsonl — analyzed post-hoc (cluster distinct frame
windows, find cells that move during W-held rows).
"""
import json, os, sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_rawall.jsonl")

import ctypes
user32 = ctypes.windll.user32
VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
def os_keys():
    s = set()
    for k, v in VK.items():
        if user32.GetAsyncKeyState(v) & 0x8000:
            s.add(k)
    return "".join(sorted(s))

def main():
    ifs = find_interfaces()
    d = hid.device()
    d.open_path(ifs["vendor"])
    d.set_nonblocking(True)
    time.sleep(0.3)
    # one-time handshake
    d.write(b"\x00" + build_sync())
    time.sleep(0.4)
    for _ in range(300):
        if not d.read(65):
            time.sleep(0.002)

    print(f"raw logger live -> {OUT} (device kept OPEN; Ctrl-C to stop)",
          flush=True)
    n = 0
    last_heartbeat = 0.0
    last_adc_log = 0.0
    f = open(OUT, "a", buffering=1)
    try:
        while True:
            now = time.time()
            # heartbeat every 2 s (keeps USB/HID link active)
            if now - last_heartbeat >= 2.0:
                try:
                    d.write(b"\x00" + build_sync())
                    last_heartbeat = now
                except OSError:
                    pass
            # ADC poll
            try:
                d.write(build_cmd(CMD_RM6X21, (6, 1)))
            except OSError:
                pass
            # drain
            for _ in range(30):
                try:
                    rep = d.read(65)
                except OSError:
                    break
                if rep:
                    b = bytes(rep)
                    if len(b) == 65 and b[0] == 0:
                        b = b[1:]
                    if b and len(b) >= 12:
                        if b[2] == 0x92:                      # ADC frame
                            cells = [int.from_bytes(b[6 + 2*i:8 + 2*i],
                                                    "little")
                                     for i in range(29)]
                            f.write(json.dumps({"t": round(now, 4),
                                                "os": os_keys(),
                                                "h": b[0],
                                                "c": cells}) + "\n")
                            n += 1
                            last_adc_log = now
                        # (0x81 SYNC replies: counted, not logged)
                else:
                    time.sleep(0.0004)
            # pace ~120 Hz
            if time.time() - now < 0.008:
                time.sleep(0.008 - (time.time() - now))
            if n and n % 2000 == 0:
                print(f"rows={n} (last ADC {time.strftime('%H:%M:%S')})",
                      flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            f.close()
        except Exception:
            pass
        print("stopped, rows logged:", n, flush=True)

if __name__ == "__main__":
    main()
