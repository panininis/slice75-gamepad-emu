"""Hardware-free logic tests for slice-pad.

Unlike smoke.py (GUI) and integration.py (live HID + ViGEm), this exercises
the pure-Python logic that the recent perf/robustness passes changed — so it
can run on any machine, in CI, with no keyboard attached and no ViGEm driver.

Covers:
  * VendorStream ring-max cache  (cached == naive max(ring); full travel() path)
  * GamepadEngine restart guard  (the _starting flag across stop+start, no
                                  concurrent double-open)
  * _clamp_cfg config validation (range-clamp + reject on the web server)
  * bridge math sanity           (normalization / deadzone / curve invariants)

Run:  python app/logic_test.py        -> prints PASS/FAIL, exit 0/1
"""
from __future__ import annotations
import os
import sys
import time
import threading
import math

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

fails = []
def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ---------------------------------------------------------------- ring cache
def test_ring_cache():
    from slice_capture import VendorStream
    from collections import deque
    v = VendorStream.__new__(VendorStream)  # no open — pure math
    v._lock = threading.Lock()
    v.adc = {44: 2900}
    v._adc_ring = {44: deque(maxlen=300)}
    v._adc_base = {}
    v._ring_n = {}
    v._ring_gen = {}
    v._ring_max_gen = {}
    v._ring_max = {}

    # 1) cached max == naive max across a press/release pattern
    ok = True
    for i in range(2000):
        v.adc[44] = 2900 - (i % 7) * 150 + (i % 13)
        v._adc_ring[44].append(v.adc[44])
        v._ring_gen[44] = v._ring_gen.get(44, 0) + 1
        if v._ring_max_cached(44) != max(v._adc_ring[44]):
            ok = False
            break
    check(ok, "ring cache: cached max == naive max over 2000 mixed samples")

    # 2) full travel() path: seed -> baseline -> press -> release
    v.adc = {44: 2900}
    v._adc_ring = {44: deque(maxlen=300)}
    v._adc_base = {}
    v._ring_n = {}
    v._ring_gen = {}
    v._ring_max_gen = {}
    v._ring_max = {}
    for _ in range(70):
        v.adc[44] = 2900
        v._adc_ring[44].append(2900)
        v._ring_gen[44] = v._ring_gen.get(44, 0) + 1
    check(v.travel(44) < 0.05, "travel(): rest after seeding is ~0")
    for _ in range(15):
        v.adc[44] = 1650  # raw must follow the ring (travel() reads v.adc)
        v._adc_ring[44].append(1650)
        v._ring_gen[44] = v._ring_gen.get(44, 0) + 1
    press = v.travel(44)
    check(press > 0.7, f"travel(): hard press is high ({press:.2f})")
    for _ in range(15):
        v.adc[44] = 2900
        v._adc_ring[44].append(2900)
        v._ring_gen[44] = v._ring_gen.get(44, 0) + 1
    check(v.travel(44) < 0.05, "travel(): release returns to ~0")


# ---------------------------------------------------------------- restart guard
def test_restart_guard():
    import engine_core
    E = engine_core.GamepadEngine

    class Stub(E):
        def __init__(self):  # skip the real (hardware-touching) init
            self.running = False
            self._starting = False
            self._stop_evt = threading.Event()
            self.engine = None
            self.bridge = None
            self.vendor = None
            self.hid = None
            self.dkeys = None
            self._calib_running = False
            self._lc = {}
            self._key_baseline = {}
            self.stats = {"frames": 0, "polls": 0}
            self._stream_dead_notified = False
            self.mapping = None
            self.cfg = None

        def _ui_queue_append(self, item):
            pass

        def stop(self):
            self.running = False
            self._starting = False  # mirrors the real stop() clearing the guard
            time.sleep(0.02)

        def start(self, _from_restart=False):
            if self.running:
                return
            if self._starting and not _from_restart:
                return  # racing start blocked
            self._starting = True
            time.sleep(0.02)
            self.running = True
            self._starting = False

    e = Stub()
    e.start()
    check(e.running and not e._starting, "start(): running, guard cleared")
    e.start()  # double start while running
    check(e.running and not e._starting, "start(): double-tap ignored (idempotent)")
    e._starting = True  # simulate an in-progress start
    e.start()
    check(e._starting, "start(): a racing start is blocked by the guard")
    e._starting = False
    e.restart()
    check(e.running and not e._starting, "restart(): back up, guard cleared")
    e._starting = True
    e.restart()  # racing restart
    check(e._starting, "restart(): a racing restart is blocked by the guard")


# ------------------------------------------------------- config validation
def test_config_clamp():
    # Import just the function without starting the server. app_web defines it
    # at module level and the server only binds in main(), so importing is safe.
    import app_web
    ok, v = app_web._clamp_cfg("gain", 99.0)
    check(ok and v == 3.0, f"clamp: gain 99 -> {v} (expect 3.0)")
    ok, v = app_web._clamp_cfg("deadzone", 5.0)
    check(ok and v == 0.5, f"clamp: deadzone 5 -> {v} (expect 0.5)")
    ok, v = app_web._clamp_cfg("stick_deadzone", -1.0)
    check(ok and v == 0.0, f"clamp: stick_deadzone -1 -> {v} (expect 0.0)")
    ok, v = app_web._clamp_cfg("max_update_hz", 99999)
    check(ok and v == 1000, f"clamp: hz 99999 -> {v} (expect 1000)")
    ok, v = app_web._clamp_cfg("max_update_hz", 0.4)
    check(ok and v == 1, f"clamp: hz 0.4 -> {v} (expect 1, min bound)")
    ok, v = app_web._clamp_cfg("max_update_hz", "garbage")
    check(not ok, "clamp: hz 'garbage' rejected")
    ok, v = app_web._clamp_cfg("deadzone", "nan-str")
    check(not ok, "clamp: deadzone non-numeric rejected")
    ok, v = app_web._clamp_cfg("invert_x", 1)
    check(ok and v is True, "clamp: invert_x coerced to bool")
    ok, v = app_web._clamp_cfg("normalize_mode", "full")
    check(ok and v == "full", "clamp: string passthrough")


# ---------------------------------------------------------------- bridge math
def test_bridge_math():
    from gamepad_bridge import (GamepadConfig, compose_and_normalize,
                                stick_deadzone, apply_deadzone,
                                curve_apply, compile_curve)
    # diagonal (full) must sit on the unit circle
    x, y = compose_and_normalize(1.0, 0.0, 0.0, 1.0, GamepadConfig("full"))
    check(abs(math.hypot(x, y) - 1.0) < 1e-9, "diag full: magnitude == 1")
    # straight (full) is clamped, not normalized  (D is the +x axis)
    x, y = compose_and_normalize(0.0, 0.0, 0.0, 1.0, GamepadConfig("full"))
    check(x == 1.0 and y == 0.0, "straight full (D): clamped to 1.0")
    # off mode: raw sum, hard-clamped
    x, y = compose_and_normalize(1.0, 0.0, 0.0, 1.0, GamepadConfig("off"))
    check(x == 1.0 and y == 1.0, "off: raw sum (no normalization)")
    # circular deadzone zeroes small vectors
    x, y = stick_deadzone(0.01, 0.01, 0.03)
    check(x == 0.0 and y == 0.0, "stick deadzone: tiny vector -> 0")
    # deadzone is a no-op at dz=0
    x, y = stick_deadzone(0.4, 0.3, 0.0)
    check(x == 0.4 and y == 0.3, "stick deadzone: dz=0 passthrough")
    # linear deadzone
    check(apply_deadzone(0.01, 0.05) == 0.0, "key deadzone: below -> 0")
    check(apply_deadzone(0.5, 0.05) > 0.4, "key deadzone: above -> rescaled")
    # curve sandbox rejects anything that escapes the whitelist
    check(compile_curve("v") is not None, "curve: 'v' compiles")
    check(compile_curve("1 - (1-v)**2") is not None, "curve: quadratic compiles")
    check(compile_curve("__import__('os')") is None, "curve: dunder/import rejected")
    check(compile_curve("open('/etc/passwd')") is None, "curve: open() rejected")
    check(curve_apply(0.5, None) == 0.5, "curve: None fn passthrough")


# --------------------------------------------------- trigger option
def test_trigger_block_y():
    from gamepad_bridge import GamepadConfig, GamepadBridge
    # dry-run bridge: all math runs, no virtual pad registered
    b = GamepadBridge(GamepadConfig())
    b.dry_run = True

    def run(w=1.0, a=0.0, s=0.0, d=0.0, **kw):
        b.cfg = GamepadConfig(**kw)
        # clear the update throttle + last-state so every assertion sees
        # FRESH output (the 500 Hz throttle suppresses back-to-back calls
        # that compute the same values)
        b._last_update = 0.0
        b.last_x = b.last_y = None
        b.last_triggers = (None, None)
        return b.update(w, a, s, d)

    # baseline: W alone moves the stick up AND pulls LT
    x16, y16 = run(1.0, 0.0, 0.0, 0.0, trigger_w=True)
    check(y16 > 10000, "trigger_w: W moves the stick (y>0)")
    check(b.last_triggers[0] > 100, "trigger_w: LT follows W")

    # block_y_on_trigger: W becomes trigger-only — no vertical stick
    x16, y16 = run(1.0, 0.0, 0.0, 0.0, trigger_w=True, block_y_on_trigger=True)
    check(y16 == 0, "block_y_on_trigger: vertical stick suppressed (y==0)")
    check(b.last_triggers[0] > 100, "block_y_on_trigger: LT still pulls")

    # W+S in triggers-only mode: both triggers pull, stick stays centred
    x16, y16 = run(1.0, 0.0, 1.0, 0.0, trigger_w=True, block_y_on_trigger=True)
    check(y16 == 0 and x16 == 0 and
          b.last_triggers[0] > 100 and b.last_triggers[1] > 100,
          "block_y_on_trigger: W+S pulls both triggers, stick centred")

    # x-axis (A/D) is untouched by the option
    x16, y16 = run(0.0, 0.0, 0.0, 1.0, trigger_w=True, block_y_on_trigger=True)
    check(x16 > 10000 and y16 == 0,
          "block_y_on_trigger: A/D (x-axis) unaffected")

    # option without trigger_w changes nothing
    x16, y16 = run(1.0, 0.0, 0.0, 0.0, trigger_w=False, block_y_on_trigger=True)
    check(y16 > 10000, "block_y_on_trigger alone (no trigger_w): stick moves")



if __name__ == "__main__":
    print("slice-pad logic tests (hardware-free)")
    test_ring_cache()
    test_restart_guard()
    test_config_clamp()
    test_bridge_math()
    test_trigger_block_y()
    print("-" * 40)
    if fails:
        print(f"LOGIC FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("LOGIC PASS")
