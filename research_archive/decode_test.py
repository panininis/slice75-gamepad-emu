"""Unit test for the new HID usage-based key decoding + convention
auto-detection in DigitalKeys (no hardware needed)."""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))

from slice_capture import DigitalKeys, _USAGE2K  # noqa: E402

failures: list[str] = []

dk = DigitalKeys(b"dummy")

# --- pure decode checks -------------------------------------------------
# flat convention: W = usage 0x1A -> bit 26 -> byte 3, bit 2
kb_flat = bytearray(12)
kb_flat[3] = 0b00000100
f, s, bits = dk._decode_bits(bytes(kb_flat))
if "W" not in f:
    failures.append(f"flat decode W failed: {f}")
if "W" in s:
    failures.append(f"flat decode mis-assigned to spec: {s}")

# spec convention: W at usage 0x1A -> bit 0x1A-0x04=22 -> byte 2, bit 6
kb_spec = bytearray(12)
kb_spec[2] = 0b1000000
f2, s2, _ = dk._decode_bits(bytes(kb_spec))
if "W" not in s2:
    failures.append(f"spec decode W failed: {s2}")
if "W" in f2:
    failures.append(f"spec decode mis-assigned to flat: {f2}")

# multiple keys: spec-convention W+D (D=usage 0x07 -> bit 3; W bit 22)
kb_wd = bytearray(12)
kb_wd[0] = 0b00001000
kb_wd[2] = 0b1000000
f3, s3, _ = dk._decode_bits(bytes(kb_wd))
if s3 != {"W", "D"}:
    failures.append(f"W+D spec decode wrong: {s3}")

# --- convention auto-detection -------------------------------------------
# A) OS-grounded detection (the real path in the app): reports are observed,
#    then the OS-held key set is fed in to confirm the convention.
dk2 = DigitalKeys(b"dummy")
dk2.latest = set()
dk2._conv = None
f, s, _ = dk2._decode_bits(bytes(kb_wd))         # W+D pressed (spec-packed)
dk2._update_convention(f, s, set())
dk2._last_labels = {"flat": f, "spec": s}
dk2.observe({"W", "D"})                          # OS confirms W+D held
if dk2._conv != 1:
    failures.append(f"OS-grounded conv should lock spec(1), got {dk2._conv}")
else:
    print("auto-detect (OS W+D held, spec report) ->", dk2.convention)

dk3 = DigitalKeys(b"dummy")
dk3.latest = set()
dk3._conv = None
f, s, _ = dk3._decode_bits(bytes(kb_flat))       # flat-packed W press
dk3._update_convention(f, s, set())
dk3._last_labels = {"flat": f, "spec": s}
dk3.observe({"W"})                               # OS confirms W held
if dk3._conv != 0:
    failures.append(f"OS-grounded conv should lock flat(0), got {dk3._conv}")
else:
    print("auto-detect (OS W held, flat report) ->", dk3.convention)

# C) regression: observe() must read the label SETS, not the dict keys
#    (a dict unpack `flat, spec = d` yields the strings 'flat'/'spec' and
#    silently never matches).
dk6 = DigitalKeys(b"dummy")
dk6.latest = set()
dk6._conv = None
kb_w = bytearray(12)
kb_w[2] = 0b1000000  # bit 22 -> flat=S, spec=W
f, s, _ = dk6._decode_bits(bytes(kb_w))
dk6._last_labels = {"flat": f, "spec": s}
dk6.observe({"W"})
if dk6._conv != 1:
    failures.append(f"regression: observe() should lock spec(1), "
                    f"got {dk6._conv} (os_hits={dk6._os_hits})")
else:
    print("regression check: observe() reads sets, locks correctly")

# B) pure transition scoring: repeated transitions on one convention win
#    bit 39 (usage 0x27='0'): labeled under flat, unlabeled under spec
dk5 = DigitalKeys(b"dummy")
dk5.latest = set()
dk5._conv = None
kb0_flat = bytearray(12)
kb0_flat[4] = 0b10000000  # bit 39
for _ in range(3):
    f, s, _ = dk5._decode_bits(bytes(kb0_flat))
    assert "0" in f and not s, (f, s)
    dk5._update_convention(f, s, set())           # "pressed" (prev empty)
if dk5._conv != 0:
    failures.append(f"repeated flat transitions should lock 0, got {dk5._conv}")
else:
    print("auto-detect (repeated flat transitions) ->", dk5.convention)

# --- usage table sanity ----------------------------------------------------
for u, k in ((0x04, "A"), (0x16, "S"), (0x1A, "W"), (0x07, "D")):
    if _USAGE2K.get(u) != k:
        failures.append(f"usage {u:#x} -> {_USAGE2K.get(u)} != {k}")
print("usage table OK (W=0x1A, A=0x04, S=0x16, D=0x07)")

if failures:
    print("DECODE TEST FAIL:")
    for x in failures:
        print("  -", x)
    sys.exit(1)
print("DECODE TEST PASS")
