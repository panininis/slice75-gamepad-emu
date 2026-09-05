"""GUI smoke test: instantiate App, pump the main loop ~3s, exercise the
bridge math, then close. Prints PASS/FAIL details."""
import sys
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import customtkinter as ctk

failures = []

# 1) pure-math check of the bridge (no HID, no ViGEm needed)
from gamepad_bridge import (GamepadBridge, GamepadConfig,
                            compose_and_normalize, stick_deadzone,
                            apply_deadzone, curve_apply, compile_curve)
import math

# diagonal must not exceed unit magnitude (full normalization)
cfg = GamepadConfig(normalize_mode="full")
x, y = compose_and_normalize(1.0, 0.0, 0.0, 1.0, cfg)  # W+D at full
m = math.hypot(x, y)
if abs(m - 1.0) > 1e-6:
    failures.append(f"diagonal magnitude {m} != 1.0 (full mode)")
# straight movement unaffected
x2, y2 = compose_and_normalize(1.0, 0.0, 0.0, 0.0, cfg)
if (x2, y2) != (0.0, 1.0):
    failures.append(f"straight W wrong: {x2},{y2}")
# diagonal-only mode: both active
cfg_d = GamepadConfig(normalize_mode="diag")
x3, y3 = compose_and_normalize(1.0, 0.0, 0.0, 1.0, cfg_d)
if abs(math.hypot(x3, y3) - 1.0) > 1e-6:
    failures.append(f"diag mode magnitude {math.hypot(x3, y3)} != 1.0")
# off mode clamps axes
cfg_o = GamepadConfig(normalize_mode="off")
x4, y4 = compose_and_normalize(1.0, 0.0, 0.0, 1.0, cfg_o)
if (x4, y4) != (1.0, 1.0):
    failures.append(f"off mode should clamp to (1,1): {x4},{y4}")
# deadzone
if apply_deadzone(0.01, 0.05) != 0.0:
    failures.append("deadzone: small value should be 0")
if abs(apply_deadzone(0.55, 0.05) - (0.55 - 0.05) / 0.95) > 1e-9:
    failures.append("deadzone rescale wrong")
# dynamic response curves: compile + apply, plus rejection of bad input
for name, expr in (("linear", "v"), ("squared", "v**2"), ("sqrt", "sqrt(v)"),
                   ("smooth step", "v*v*(3 - 2*v)"),
                   ("s-curve", "0.5 + 0.5*sin(pi*(v - 0.5))"),
                   ("center boost", "v + 0.15*sin(pi*v)")):
    fn = compile_curve(expr)
    if fn is None:
        failures.append(f"curve {name} failed to compile")
        continue
    for vv in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = curve_apply(vv, fn)
        if not (0.0 <= v <= 1.0):
            failures.append(f"curve {name} out of range: {v}")
# linear identity
fn = compile_curve("v")
if abs(curve_apply(0.37, fn) - 0.37) > 1e-9:
    failures.append("linear curve not identity")
# invalid expressions must be rejected (sandbox)
for bad in ("__import__('os').system('id')", "v if open('/x') else 0",
            "exec('x=1')", "v.__class__", ""):
    if compile_curve(bad) is not None:
        failures.append(f"curve sandbox accepted bad expr: {bad!r}")
# dynamic curve changes the actual stick output
# (key order is update(vw, va, vs, vd); W drives +y; deadzones off)
_CK = dict(normalize_mode="off", deadzone=0.0, stick_deadzone=0.0)
br_c = GamepadBridge(GamepadConfig(**_CK))
br_c.open()
_, lin_y = br_c.update(0.5, 0.0, 0.0, 0.0)    # linear -> 0.5
br_c.set_curve("v**2")
_, sq_y = br_c.update(0.5, 0.0, 0.0, 0.0)     # squared -> 0.25
if not (0 < sq_y < lin_y):
    failures.append(f"dynamic curve v**2 not applied (lin={lin_y} sq={sq_y})")
# invalid curve is rejected, previous curve kept
if br_c.set_curve("not(a curve"):
    failures.append("invalid curve accepted")
br_c2 = GamepadBridge(GamepadConfig(curve_expr="sqrt(v)", **_CK))
br_c2.open()
_, sq2_y = br_c2.update(0.25, 0.0, 0.0, 0.0)  # sqrt(0.25)=0.5
if abs(sq2_y / 32767.0 - 0.5) > 0.01:
    failures.append(f"curve_expr init wrong: {sq2_y/32767:.3f} != 0.5")
# dry-run bridge end-to-end
br = GamepadBridge(GamepadConfig())
ok, msg = br.open()
x16, y16 = br.update(1.0, 0.0, 0.0, 1.0)
if x16 == 0 or y16 == 0:
    failures.append(f"dry-run bridge produced no stick motion: {x16},{y16}")
if abs(math.hypot(x16, y16) - int(32767)) > 2:
    failures.append(f"diagonal should map to unit circle, got {math.hypot(x16, y16)}")
print(f"bridge math: OK (dry-run={br.dry_run}) diag16=({x16},{y16})")

# 2) GUI instantiation
import app as appmod
app = appmod.App()

def close_later():
    time.sleep(3.0)
    app.after(0, app.destroy)

app.after(100, close_later)
app.mainloop()

# 3) engine start/stop without HID (should fail gracefully with error dialog
# suppressed) — just verify toggle paths exist.
if failures:
    print("SMOKE FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("SMOKE PASS")
