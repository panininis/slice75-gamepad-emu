#!/usr/bin/env python3
"""Ground-truth WASD calibration for the Slice75 HE.
Simultaneously logs:
  * digital key state (MI_01 Col01 scancodes) -> which key is physically down
  * 6x8-bit analog block (MI_00, bytes 2-7)
  * vendor 126-key real-time travel matrix (MI_02, CMD 18 sub 2=mm, sub 3=press)
Duration = argv[1] seconds (default 45). Press & HOLD W, A, S, D one at a time.
At the end it prints, for each key pressed, which analog byte(s) and which
matrix position(s) spiked, plus the observed value scale.
"""
import ctypes, ctypes.wintypes, time, sys
import hid as hidapi

VID, PID = 0x1CA3, 0x0701

def find(m):
    for d in hidapi.enumerate():
        if d.get('vendor_id') == VID and d.get('product_id') == PID and m in d['path']:
            return d
    return None

mi00 = find(b'MI_00')
mi01c1 = None
for d in hidapi.enumerate():
    if d.get('vendor_id') == VID and d.get('product_id') == PID and b'MI_01' in d['path'] and b'Col01' in d['path']:
        mi01c1 = d
vendor = find(b'MI_02')
print(f"MI_00={bool(mi00)} MI_01Col01={bool(mi01c1)} vendor={bool(vendor)}", flush=True)
assert mi00 and mi01c1 and vendor, "missing interface(s)"

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
CF = k32.CreateFileW
CF.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
               ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE]
CF.restype = ctypes.wintypes.HANDLE
rh = CF(vendor['path'].decode(), 0xC0000000, 0x3, None, 3, 0, None)
if rh and rh != 0xFFFFFFFFFFFFFFFF:
    k32.CancelIoEx(rh, None); k32.CloseHandle(rh)
time.sleep(0.3)

kh = hidapi.device(); kh.open_path(mi00['path']); kh.set_nonblocking(True)
kh2 = hidapi.device(); kh2.open_path(mi01c1['path']); kh2.set_nonblocking(True)
vh = hidapi.device(); vh.open_path(vendor['path']); vh.set_nonblocking(True)
print("interfaces opened", flush=True)

def body64(cmd, params=()):
    r = bytearray(64); a = 4
    for v in params: r[a] = v; a += 1
    r[0] = 0x5C; r[2] = 0; r[1] = a - 4
    r[3] = (53 + r[0] + r[1] + r[2] + r[r[1] + 3]) & 0xFF
    return bytes(r)
def sync_body():
    r = bytearray(64); a = 4
    for v in (1, 2, 3, 4): r[a] = v; a += 1
    r[0] = 0x5C; r[2] = 1; r[1] = a - 4
    r[3] = (53 + r[0] + r[1] + r[2] + r[r[1] + 3]) & 0xFF
    return bytes(r)

vh.write(b'\x00' + sync_body())
time.sleep(0.4)
t0 = time.time()
while time.time() - t0 < 0.4:
    vh.read(65); time.sleep(0.002)
print("STANDBY: press & hold W any time in the next 10 minutes. Capture starts automatically.", flush=True)

# Set-1 scancode -> label for the keys we care about (rows near WASD)
SC2K = {}
for i, ch in enumerate("1234567890"):
    SC2K[0x02 + i] = ch
for i, ch in enumerate("QWERTYUIOP"):
    SC2K[0x10 + i] = ch
for i, ch in enumerate("ASDFGHJKL"):
    SC2K[0x1E + i] = ch
for i, ch in enumerate("ZXCVBNM"):
    SC2K[0x2C + i] = ch
SC2K.update({0x1C: 'ENTER', 0x3A: 'SPACE', 0xE0: 'SHIFT_L', 0xE4: 'SHIFT_R',
             0x50: 'UP', 0x51: 'DOWN', 0x4F: 'LEFT', 0x52: 'RIGHT', 0x1D: 'CTRL_L'})

def read_keys_once():
    ks = set()
    try:
        rep1 = kh2.read(16)
    except OSError:
        rep1 = None
    if rep1:
        d1 = rep1[1:] if (len(rep1) >= 14 and rep1[0] == 1) else rep1
        if len(d1) >= 13:
            kb = d1[1:13]
            for bi in range(12):
                for bit in range(8):
                    if (kb[bi] >> bit) & 1:
                        code = bi * 8 + bit
                        if code in SC2K:
                            ks.add(SC2K[code])
    return ks

print("waiting for first key press (up to 600s)...", flush=True)
wait_end = time.time() + 600
first = None
while time.time() < wait_end:
    ks = read_keys_once()
    if ks:
        first = ks
        break
    time.sleep(0.005)
if not first:
    print("ABORT: no key press detected within the standby window", flush=True)
    sys.exit(1)
print(f"first press {sorted(first)} -> capturing 40s. Hold W, then A, then S, then D (each ~3s).", flush=True)
vh.write(b'\x00' + sync_body())
time.sleep(0.2)
vh.write(b'\x00' + body64(18, (3, 1)))
time.sleep(0.1)

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
t_start = time.time()
import json
raw_log = []  # (t, frozenset(keys), analog_tuple)
last_log_t = 0.0
analog_max = [0] * 6
key_analog = {}            # key -> {byteidx: max}
key_mat = {}               # key -> {pos: max(mm)}
key_matpress = {}          # key -> {pos: max(press)}
cur_keys = set()
vendor_frames = 0
sub_count = {}
mm_acc = {}    # cumulative latest mm per matrix position
press_acc = {} # cumulative latest press per matrix position
last_poll = 0.0
poll_interval = 0.020
analog_report_count = 0
nonzero_analog_events = 0

def read_vendor_matrix():
    """Drain pending vendor frames; return the cumulative (mm, press) dicts."""
    global vendor_frames
    while True:
        rep = vh.read(65)
        if not rep or len(rep) < 6:
            break
        data = rep[1:] if (rep[0] == 0 and rep[1] == 0x5C) else rep
        if data[0] != 0x5C:
            continue
        L = data[1]
        if L == 0x92: L = 192
        if L > len(data):
            continue
        cls = data[2]
        if cls == 0x92:
            vendor_frames += 1
            # frame layout: [0]=0x5C [1]=len [2]=class [3]=cs [4]=d0
            #               [5]=sub (2=mm,3=press) [6..]=matrix (63 keys)
            # responses arrive in request order (we request half1 then half2)
            sub = data[5] if len(data) > 5 else None
            half = 1 if (vendor_frames % 2 == 1) else 2
            base = 0 if half == 1 else 63
            body = data[6:]
            if sub == 2:  # mm 16-bit LE (0.001 mm)
                sub_count[2] = sub_count.get(2, 0) + 1
                for i in range(min(63, len(body) // 2)):
                    pos = base + i
                    if pos < 126:
                        mm_acc[pos] = int.from_bytes(body[i * 2:i * 2 + 2], 'little')
            elif sub in (3, 6):  # press 8-bit / raw ADC
                sub_count[3] = sub_count.get(3, 0) + 1
                if sub == 3:
                    for i in range(min(63, len(body))):
                        pos = base + i
                        if pos < 126:
                            press_acc[pos] = body[i]
    return mm_acc, press_acc

while time.time() - t_start < DUR:
    now = time.time()
    # poll vendor
    if now - last_poll > poll_interval:
        last_poll = now
        try:
            vh.write(b'\x00' + body64(18, (2, 1)))
            vh.write(b'\x00' + body64(18, (2, 2)))
        except Exception:
            pass
    mm, press = read_vendor_matrix()
    # MI_00 analog
    try:
        rep0 = kh.read(16)
    except OSError:
        rep0 = None
    analog = None
    if rep0:
        d = rep0[1:] if len(rep0) == 9 and rep0[0] == 0 else rep0
        if len(d) >= 8:
            analog = list(d[2:8])
            analog_report_count += 1
            if any(analog):
                nonzero_analog_events += 1
            for b_idx, v in enumerate(analog):
                if v > 0:
                    analog_max[b_idx] = max(analog_max[b_idx], v)
                    for k in cur_keys:
                        ka = key_analog.setdefault(k, {})
                        ka[b_idx] = max(ka.get(b_idx, 0), v)
    # MI_01 Col01 digital keys
    try:
        rep1 = kh2.read(16)
    except OSError:
        rep1 = None
    ks = set()
    if rep1:
        d1 = rep1[1:] if (len(rep1) >= 14 and rep1[0] == 1) else rep1
        if len(d1) >= 13:
            kb = d1[1:13]
            for bi in range(12):
                for bit in range(8):
                    if (kb[bi] >> bit) & 1:
                        code = bi * 8 + bit
                        if code in SC2K:
                            ks.add(SC2K[code])
    if ks != cur_keys:
        cur_keys = ks
        if ks and (mm or press):
            for k in ks:
                km = key_mat.setdefault(k, {})
                for p, v in mm.items():
                    if v > 0:
                        km[p] = max(km.get(p, 0), v)
                kp = key_matpress.setdefault(k, {})
                for p, v in press.items():
                    if v > 0:
                        kp[p] = max(kp.get(p, 0), v)
    if analog is not None and (now - last_log_t > 0.005):
        last_log_t = now
        raw_log.append((round(now - t_start, 4), sorted(cur_keys), analog))
    time.sleep(0.001)

kh.close(); kh2.close(); vh.close()
try:
    with open('wasd_raw.json', 'w') as _f:
        json.dump(raw_log, _f)
    print(f"raw samples: {len(raw_log)} -> wasd_raw.json", flush=True)
except Exception as _e:
    print("raw log write failed", _e, flush=True)

print(f"\n=== RESULT (duration {DUR:.0f}s) ===", flush=True)
print(f"analog reports: {analog_report_count}  nonzero-analog events: {nonzero_analog_events}")
print(f"vendor 146 frames: {vendor_frames}  (sub2={sub_count[2]}, sub3={sub_count[3]})")
print(f"\n6-byte analog maxima: {analog_max}")
print("\n--- KEY -> 6-byte analog (top) ---", flush=True)
for k, bb in sorted(key_analog.items()):
    top = sorted(bb.items(), key=lambda x: -x[1])[:4]
    print(f"  {k:>8}: {top}")
print("\n--- KEY -> vendor matrix position (mm, top) ---", flush=True)
for k, bb in sorted(key_mat.items()):
    top = sorted(bb.items(), key=lambda x: -x[1])[:5]
    print(f"  {k:>8}: " + ", ".join(f"pos{p}={v}mm" for p, v in top))
print("\n--- KEY -> vendor matrix position (press 0-255, top) ---", flush=True)
for k, bb in sorted(key_matpress.items()):
    top = sorted(bb.items(), key=lambda x: -x[1])[:5]
    print(f"  {k:>8}: " + ", ".join(f"pos{p}={v}" for p, v in top))
print("done", flush=True)
