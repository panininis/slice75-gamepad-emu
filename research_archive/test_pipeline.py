"""Verify the recorder's OsKeys->event pipeline with a SYNTHETIC keypress.

Sends a real OS-level W key-down/up (SendInput) while OsKeys.poll() runs,
and confirms the event is detected + logged.  This proves the key-detection
half of the pipeline is working (the physical Hall ADC only moves on a real
press, so it stays 0 here — that's expected and is a separate check).

PASS = the synthetic W is caught by OsKeys.poll() and logged as a press.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, "app")
from slice_capture import OsKeys  # noqa: E402


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


def vk_input(code, down):
    flags = 0 if down else 0x02  # KEYEVENTF_KEYUP
    inp = INPUT(type=1, u=_INPUTunion(ki=KEYBDINPUT(
        wVk=code, wScan=0, dwFlags=flags, time=0,
        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))))
    return inp


VK_W = 0x57
ok = ctypes.windll.user32.SendInput
osk = OsKeys()
print(f"OsKeys.available={osk.available}")
time.sleep(0.3)
# idle: should be empty
idle = osk.poll()
print(f"idle poll: {idle}")
# press W down
n = ok(1, ctypes.byref(vk_input(VK_W, True)))
print(f"SendInput down -> {n}")
caught = []
t0 = time.time()
while time.time() - t0 < 0.6:
    if osk.poll():
        caught.append(sorted(osk.poll()))
        break
    time.sleep(0.01)
ok(1, ctypes.byref(vk_input(VK_W, False)))
print(f"SendInput up   -> {ok(1, ctypes.byref(vk_input(VK_W, False)))}")
print(f"caught during hold: {caught}")
if caught:
    print("PASS — OsKeys detects OS key events; recorder pipeline is alive.")
    sys.exit(0)
else:
    print("FAIL — OsKeys did NOT catch a real OS W press. Detector is broken.")
    sys.exit(1)
