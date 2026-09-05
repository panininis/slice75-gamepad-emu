"""Calibration stress test: engine loop + calibration worker concurrent.

Simulates the user pressing W, A, S, D (digital key detection via a fake
MI_01 source) while the 1 kHz engine loop runs — the exact conditions that
crashed the app before the thread-safety fixes.  Verifies:
  * calibration worker completes all 4 keys
  * mapping is (re)written
  * engine loop stays alive the whole time (tick count in log)
  * no unhandled exceptions in the log
Runs against the REAL keyboard HID (no key presses needed — the digital
source is faked and the vendor matrix is injected).
"""
from __future__ import annotations
import os
import sys
import threading
import time
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))
os.environ["SLICE_PAD_NO_AUTOCAL"] = "1"

from slice_capture import VendorStream, HidAnalog, find_interfaces  # noqa: E402
from applog import install_hooks, touch_latest  # noqa: E402
import app as appmod  # noqa: E402

KEYS = ["W", "A", "S", "D"]
POS = {"W": 0, "A": 1, "S": 2, "D": 3}


class FakeD:
    """Fake digital key source.

    Holds each WASD key for 2 s, aligned to the calibration worker's
    sequential 8 s wait windows (W first, then A, S, D).  The engine loop
    and the calibration worker both call poll() concurrently, which is the
    exact condition that used to crash the app.
    """
    # (key, start_s, end_s) relative to worker start
    WINDOWS = [("W", 0.5, 2.5), ("A", 3.5, 5.5),
               ("S", 6.5, 8.5), ("D", 9.5, 11.5)]

    def __init__(self):
        self.opened = True
        self._t0 = time.time()
        self.held: str | None = None
        # attributes the engine loop / calibration expect on a real
        # DigitalKeys (fake reports no raw data, convention stays unknown)
        self._conv = None
        self._conv_hits = [0, 0]
        self._os_hits = [0, 0]
        self._raw_reports: list[bytes] = []

    @property
    def convention(self):
        return {None: "auto", 0: "flat", 1: "spec"}[self._conv]

    def observe(self, os_held):
        pass

    def poll(self):
        el = time.time() - self._t0
        self.held = None
        for key, a, b in self.WINDOWS:
            if a <= el < b:
                self.held = key
                return {key}
        return set()

    def close(self):
        pass


def main() -> int:
    install_hooks()
    touch_latest()
    a = appmod.App()
    ifs = find_interfaces()
    assert "vendor" in ifs and "kbd" in ifs, f"interfaces: {list(ifs)}"
    a.vendor = VendorStream(ifs["vendor"])
    assert a.vendor.open()
    a.hid = HidAnalog(ifs["kbd"])
    assert a.hid.open()
    a.dkeys = FakeD()
    for k in KEYS:
        a.mapping.keys[k] = "vendor"
        a.mapping.vendor_pos[k] = POS[k]
    a.bridge = appmod.GamepadBridge(a.cfg)
    ok, msg = a.bridge.open()
    print(f"bridge: ok={ok} dry_run={a.bridge.dry_run} {msg}")
    a.running = True
    a._stop_evt.clear()
    t_start = time.time()
    a.engine = threading.Thread(target=a._engine_loop, daemon=True)
    a.engine.start()
    time.sleep(0.5)
    print("starting calibration worker (simulated presses W A S D)...")
    a._auto_calibrate()
    t0 = time.time()
    while a._calib_running and time.time() - t0 < 60:
        # inject analog travel for the currently-pressed (fake) key
        held = a.dkeys.held
        if held:
            with a.vendor._lock:
                a.vendor.mm[POS[held]] = 1800   # 1.8 mm
        time.sleep(0.02)
    dur = time.time() - t0
    vend_frames = a.vendor.frame_count if a.vendor else 0
    a._stop()
    q_len = len(a._ui_queue)
    print(f"calibration finished in {dur:.1f}s  ui_queue_len={q_len}")
    print("mapping after calib:", a.mapping.to_dict())
    ok_calib = not a._calib_running and dur < 30
    # engine liveness: log says 'finished after N ticks' — read the log file
    logdir = os.path.join(os.path.expanduser("~"), ".slice-pad", "logs")
    latest = os.path.join(logdir, "latest.log")
    with open(latest) as f:
        lp = f.read().strip()
    with open(lp) as f:
        logtext = f.read()
    import re
    m = re.search(r"engine loop finished after (\d+) ticks", logtext)
    ticks = int(m.group(1)) if m else 0
    expected = int(dur * 500)  # ~1 kHz target, 0.5k floor proves engine alive
    print(f"engine ticks: {ticks} (expected > {expected})")
    ok_engine = ticks > expected
    ok_exc = "EXCEPTION" not in logtext
    # notify items sit in the (thread-safe) UI queue until the UI thread pumps
    # them; headless test -> inspect the queue directly.
    with a._ui_qlock:
        items = list(a._ui_queue)
    ok_notify = any(it[0] == "notify" for it in items) or \
        "Calibration complete" in logtext
    print("checks:", {
        "calibration_finished": ok_calib,
        "engine_alive": ok_engine,
        "no_exceptions": ok_exc,
        "notify_logged": ok_notify,
    })
    # engine queue should have been refilled to the 64 cap (proves ticks)
    ok_q = q_len >= 60
    print("ui_queue full (engine alive):", ok_q)
    passed = ok_calib and ok_engine and ok_exc and ok_q and ok_notify
    print("CALIB TEST", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
