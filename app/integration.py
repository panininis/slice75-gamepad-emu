"""Integration test: run the app's real engine path headlessly.
Starts the engine (vendor stream + HID analog + ViGEm X360 pad), lets it
poll for ~4s, verifies vendor frames arrive, and confirms the virtual pad
registered. No key presses required (values stay ~0, but the pipeline must
tick without errors).
"""
import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app as appmod
from slice_capture import find_interfaces

fails = []

ifs = find_interfaces()
print("interfaces:", {k: v.decode().split('#')[1] for k, v in ifs.items()})
if not ifs:
    print("INTEG FAIL: no interfaces")
    sys.exit(1)

app = appmod.App()
app.withdraw()  # no visible window

# start the shared engine (bypass the GUI button) — start() now runs on a
# worker thread, so wait for the interface-open to settle.
try:
    app._eng.start()
    t_wait = time.time()
    while not app._eng.running and time.time() - t_wait < 15:
        time.sleep(0.05)
except Exception as e:
    fails.append(f"engine.start raised: {e}")

if not app.running:
    fails.append("engine did not start")

# let it run 4s
time.sleep(0.1)
app.update()
t0 = time.time()
while time.time() - t0 < 4.0:
    app.update()
    time.sleep(0.01)

vf = app.vendor.frame_count if app.vendor else 0
print(f"vendor frames in 4s: {vf}")
stream_dead = app.vendor.stream_dead if app.vendor else False
print(f"bridge dry_run: {app.bridge.dry_run if app.bridge else None}")
print(f"fw info: {app.vendor.fw_info if app.vendor else None}")
print(f"engine polls (engine loop): {app.stats['polls']}")

if stream_dead:
    # The firmware's RM6X21 (ADC) task can hang (e.g. after concurrent
    # processes fight over the vendor endpoint).  Control plane (SYNC)
    # still answers.  This is a HARDWARE state, not a code bug: unplug &
    # replug the keyboard.  Skip the live-frame check; the parser unit
    # check below still validates the code.
    print("WARN: vendor ADC stream dead (firmware hang) — unplug & replug the keyboard")
    print("      live-frame check SKIPPED (hardware state)")

if vf == 0 and not stream_dead:
    # No frames at all means the vendor stream isn't responding (endpoint
    # held by the web driver, or a parse regression).  The parser itself is
    # unit-checked below with synthetic frames, so this is a real warning.
    print("WARN: 0 vendor frames in 4s — vendor stream not responding")
    fails.append("vendor stream produced 0 frames in 4s")
else:
    print(f"vendor stream healthy: {vf} frames in 4s (~{vf/4:.0f} Hz)")
if app.bridge is not None and app.bridge.dry_run:
    fails.append("bridge fell back to dry-run (ViGEm not working)")

# force a known stick value through the bridge directly
if app.bridge and not app.bridge.dry_run:
    x16, y16 = app.bridge.update(1.0, 0.0, 0.0, 0.0)  # W full
    print(f"forced W-full -> x16={x16} y16={y16}")
    if not (20000 <= y16 <= 32767) or abs(x16) > 10:
        fails.append(f"W-full stick wrong: ({x16},{y16})")
    x16, y16 = app.bridge.update(1.0, 1.0, 1.0, 1.0)  # all four -> neutral
    print(f"all-keys -> x16={x16} y16={y16}")
    if abs(x16) > 10 or abs(y16) > 10:
        fails.append(f"all-keys should be neutral: ({x16},{y16})")

# restart check: full stop+start must revive the vendor stream (this is the
# in-place recovery for a hung ADC task — reopening the endpoint revives it).
# NOTE: the restarted engine owns a FRESH VendorStream (frame_count starts at
# 0), so compare the new instance's OWN counter advancing, vs the old one.
try:
    app._eng.restart()
    t_wait = time.time()
    while not app._eng.running and time.time() - t_wait < 15:
        time.sleep(0.05)
    time.sleep(1.5)
    new_vendor = app.vendor
    f1 = new_vendor.frame_count
    time.sleep(1.0)
    f2 = new_vendor.frame_count
    dead = bool(new_vendor.stream_dead)
    if not app._eng.running:
        fails.append("engine.restart() did not bring the engine back up")
    elif f2 <= f1:
        fails.append(f"engine.restart(): new vendor stream not advancing "
                     f"({f1} -> {f2})")
    elif dead:
        fails.append("engine.restart(): new vendor stream reports dead")
    else:
        print(f"restart ok: engine back up, fresh vendor stream {f1} -> {f2} "
              f"frames in 1 s, stream healthy")
except Exception as e:
    fails.append(f"engine.restart() raised: {e}")

app._stop()
app.update()
time.sleep(0.3)
app.update()
app.destroy()

# parser unit check: synthetic 0x92 ADC frames (real on-wire geometry).
# FW 1.1.7.3: one sub-6 answer = 2 x 64-byte reports:
#   header:      [0]=0x5C [1]=0x80 [2]=0x92 [3]=0xa3 [4]=0x00 [5]=sub
#                [6..63] = cells 0..28
#   continuation: [0..63] = cells 29..60 (raw, NO 0x5C head)
# Full frame = 61 cells.  Bank is self-classified from dead cells 12 & 14:
#   T1: cells 12,14 ALIVE (zeros at 15..20, 36..41, 57..60)
#   T2: cells 12,14 DEAD  (zeros at 12, 14..20, 22, 33, 36..41, 45..47,
#                          49..52, 58..60)
from slice_capture import VendorStream
vs = VendorStream(b'fake')

# idle templates (measured medians): T1 c0~2915, T2 c0~2981
_T1_ZERO = set(range(15, 21)) | set(range(36, 42)) | set(range(57, 61))
_T2_ZERO = {12} | set(range(14, 21)) | {22, 33} | set(range(36, 42)) | set(range(45, 48)) | set(range(49, 53)) | set(range(58, 61))

def mk_full_cells(bank: str, idx: int | None, value: int):
    """61-cell frame with one cell set to `value`, rest at idle."""
    base = 2915 if bank == "T1" else 2981
    zeros = _T1_ZERO if bank == "T1" else _T2_ZERO
    cells = []
    for i in range(61):
        if idx is not None and i == idx:
            cells.append(value)
        elif i in zeros:
            cells.append(0)
        else:
            cells.append(base + (i % 7))
    return cells

def split_frame(cells: list[int], sub: int = 6):
    """Split 61 cells into (header_report, continuation_report) bytes."""
    hdr = bytes([0x5C, 0x80, 0x92, 0xA3, 0x00, sub]) + b"".join(
        v.to_bytes(2, "little") for v in cells[:29])
    cont = b"".join(v.to_bytes(2, "little") for v in cells[29:61])
    return hdr, cont

def feed_full(bank: str, idx: int | None, value: int, sub: int = 6):
    hdr, cont = split_frame(mk_full_cells(bank, idx, value), sub)
    vs._process_92(hdr, cont=cont)

# Rest frames first (the engine always sees several seconds of rest before
# a keypress, so the running-max baseline settles at the rest value).
# T1 idx 0 -> pos 0 ; T2 idx 5 -> pos 66 ; W = T1 idx 44 -> pos 44.
for _ in range(70):
    feed_full("T1", 0, 2915)
    feed_full("T2", 5, 2981)
# sanity: the classifier must have routed T1->pos 0, T2->pos 66
assert vs.adc.get(0) is not None, "T1 idx0 did not map to pos 0"
assert vs.adc.get(66) is not None, "T2 idx5 did not map to pos 66"
assert vs.adc.get(44) is not None, "T1 idx44 (W, continuation) not present"
# travel() seeds its baseline on the FIRST call per sensor (returns 0.0),
# so seed now, while everything is at rest.
tr_rest = vs.travel(66)
assert tr_rest < 0.05, f"rest travel should be ~0, got {tr_rest}"
tr_w_seed = vs.travel(44)
assert tr_w_seed < 0.05, f"W rest travel should be ~0, got {tr_w_seed}"
tr0_seed = vs.travel(0)
assert tr0_seed < 0.05, f"pos0 rest travel should be ~0, got {tr0_seed}"
# press pos 0, check immediately (a later full frame rewrites the rest)
feed_full("T1", 0, 1500)
tr0 = vs.travel(0)
if vs.adc.get(0) != 1500:
    fails.append(f"vendor parser: adc[0] should be 1500 (pressed), got {vs.adc.get(0)}")
if not (0.9 <= tr0 <= 1.0):
    fails.append(f"travel(): full press at pos0 should be ~1.0, got {tr0:.3f}")
# press pos 66 (T2), check immediately
feed_full("T2", 5, 1750)
tr66 = vs.travel(66)
if vs.adc.get(66) != 1750:
    fails.append(f"vendor parser: adc[66] should be 1750, got {vs.adc.get(66)}")
if not (0.9 <= tr66 <= 1.0):
    fails.append(f"travel(): full press at pos66 should be ~1.0, got {tr66:.3f}")
# W: press the CONTINUATION cell (the regression this whole fix is for)
feed_full("T1", 44, 1715)
tr_w = vs.travel(44)
print(f"parser check: frame_count={vs.frame_count} adc[0]={vs.adc.get(0)} "
      f"adc[66]={vs.adc.get(66)} adc[44]={vs.adc.get(44)} "
      f"trW={tr_w:.3f}")
if vs.adc.get(44) != 1715:
    fails.append(f"vendor parser: adc[44] (W continuation cell) should be 1715, "
                 f"got {vs.adc.get(44)}")
# W: rest ~2917 -> 1715 press ~ delta ~1200 -> ~0.96
if not (0.75 <= tr_w <= 1.0):
    fails.append(f"travel(): W press at pos44 should be ~0.8-1.0, got {tr_w:.3f}")
# a different rest sensor (never pressed) stays ~0
tr_rest2 = vs.travel(61)
if tr_rest2 > 0.1:
    fails.append(f"travel(): rest sensor should be ~0, got {tr_rest2:.3f}")

if fails:
    print("INTEG FAIL:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("INTEG PASS")
