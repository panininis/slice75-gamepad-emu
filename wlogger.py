"""Self-reviving persistent W-logger.

The board's RM6X21 ADC stream goes SILENT after ~30s-1min of polling
(active-sleep / ADC task hang). Reopening the HID device revives it
(verified repeatedly). So this logger:
  * polls RM6X21 at ~120 Hz, logging EVERY 0x92 frame (both banks — the
    board self-cycles A/B; classify post-hoc by dead-cell pattern)
  * logs the OS WASD-held set each frame
  * sends a SYNC heartbeat every 2 s
  * if NO ADC frame for 5 s -> close + reopen the device (revive) and log
    a revive marker, then continue
Runs forever until killed. You press W (and A/S/D) on the Slice whenever
you're at the PC, then message me.

Output: wasd_Wlog.jsonl
"""
import json, os, sys, time
sys.path.insert(0, "app")
import hid
from slice_capture import build_cmd, build_sync, find_interfaces, CMD_RM6X21

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wasd_Wlog.jsonl")

import ctypes
user32 = ctypes.windll.user32
VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
def os_keys():
    s = set()
    for k, v in VK.items():
        if user32.GetAsyncKeyState(v) & 0x8000:
            s.add(k)
    return "".join(sorted(s))

def open_dev(ifs):
    d = hid.device()
    d.open_path(ifs["vendor"])
    d.set_nonblocking(True)
    time.sleep(0.3)
    d.write(b"\x00" + build_sync())
    time.sleep(0.4)
    for _ in range(300):
        if not d.read(65):
            time.sleep(0.002)
    return d

def main():
    ifs = find_interfaces()
    f = open(OUT, "a", buffering=1)
    d = open_dev(ifs)
    print(f"W-logger live -> {OUT} (self-reviving; Ctrl-C to stop)", flush=True)
    n = 0
    last_adc = time.time()
    last_hb = 0.0
    revives = 0
    try:
        while True:
            now = time.time()
            if now - last_hb >= 2.0:
                try:
                    d.write(b"\x00" + build_sync())
                except OSError:
                    pass
                last_hb = now
            try:
                d.write(b"\x00" + build_cmd(CMD_RM6X21, (6, 1)))
            except OSError:
                pass
            for _ in range(25):
                try:
                    rep = d.read(65)
                except OSError:
                    break
                if rep:
                    b = bytes(rep)
                    if len(b) == 65 and b[0] == 0:
                        b = b[1:]
                    if b and len(b) >= 12 and b[2] == 0x92:
                        cells = [int.from_bytes(b[6+2*i:8+2*i], "little")
                                 for i in range(29)]
                        f.write(json.dumps({"t": round(now, 4), "os": os_keys(),
                                            "h": b[0], "c": cells}) + "\n")
                        n += 1
                        last_adc = now
                else:
                    time.sleep(0.0004)
            # self-revive: silent ADC for >5 s -> reopen the device
            if time.time() - last_adc > 5.0:
                revives += 1
                f.write(json.dumps({"t": round(time.time(), 4), "revive": revives,
                                    "os": os_keys()}) + "\n")
                print(f"ADC silent -> REVIVE #{revives} "
                      f"{time.strftime('%H:%M:%S')}", flush=True)
                try:
                    d.close()
                except OSError:
                    pass
                time.sleep(0.4)
                d = open_dev(ifs)
                last_adc = time.time()
            if time.time() - now < 0.008:
                time.sleep(0.008 - (time.time() - now))
            if n and n % 2000 == 0:
                print(f"rows={n} revives={revives} "
                      f"(last ADC {time.strftime('%H:%M:%S')})", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            f.close()
        except Exception:
            pass
        try:
            d.close()
        except Exception:
            pass
        print(f"stopped, rows={n} revives={revives}", flush=True)

if __name__ == "__main__":
    main()
