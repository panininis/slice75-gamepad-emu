"""Re-dump MI_00/MI_01/MI_02 HID report descriptors from the live board."""
from __future__ import annotations
import sys

sys.path.insert(0, "app")
import hid  # noqa: E402

VID, PID = 0x1CA3, 0x0701
for d in hid.enumerate(VID, PID):
    p = d["path"]
    tag = "MI02" if b"MI_02" in p else "MI00" if b"MI_00" in p else \
          "MI01C1" if (b"MI_01" in p and b"Col01" in p) else \
          "MI01C?" if b"MI_01" in p else "?"
    try:
        dev = hid.device()
        dev.open_path(p)
        desc = bytes(dev.get_report_descriptor())
        dev.close()
        print(f"== {tag} len={len(desc)} ==")
        print(bytes(desc).hex(" "))
        print()
    except Exception as e:
        print(tag, "ERROR", e)
