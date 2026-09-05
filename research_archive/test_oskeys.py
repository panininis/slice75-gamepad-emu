"""Verify the FIXED OsKeys detects a W press.

Sends a real (SendInput) W keydown, polls OsKeys, then keyup.
Confirms the VK-code fix actually makes OS keypress detection work.
"""
from __future__ import annotations
import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, "app")
from slice_capture import OsKeys  # noqa: E402


class _KEYBD(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSE(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWARE(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.UINT), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSE), ("ki", _KEYBD), ("hi", _HARDWARE)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


u32 = ctypes.windll.user32


def send_vk(vk, flag):
    i = INPUT()
    i.type = 1
    i.u.ki.wVk = vk
    i.u.ki.dwFlags = flag
    return u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def main():
    osk = OsKeys()
    print(f"OsKeys.available={osk.available}  VK map: {OsKeys._VK}")
    print(f"  idle: {osk.poll()}")
    # press W for ~0.5s while polling
    send_vk(OsKeys._VK["W"], 0)  # down
    seen = set()
    t = time.time()
    while time.time() - t < 0.6:
        seen |= osk.poll()
        time.sleep(0.02)
    send_vk(OsKeys._VK["W"], 2)  # up
    print(f"  during W hold, OsKeys saw: {seen or 'NONE'}")
    print(f"  after release: {osk.poll()}")
    ok = "W" in seen
    print(f"\nRESULT: {'PASS — OsKeys detects W' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
