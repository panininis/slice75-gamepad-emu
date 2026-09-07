"""Gamepad bridge: WASD analog -> virtual XInput (Xbox 360) stick.

Implements diagonal vector normalization so that moving diagonally
(W+D) is not ~41% faster than straight movement, plus per-axis shaping:
deadzone (linear), gain, response curve, and stick/axis remapping.

Pipeline per poll (default 1000 Hz cap, driven by the UI loop):
  raw travel 0..1 per key
    -> per-key deadzone & gain & curve      (axis shaping)
    -> compose (x, y) from the four keys
    -> diagonal vector normalization:
         m = sqrt(x^2 + y^2); if m > 1: x/=m, y/=m
         (with a `normalize_mode` choice:
          'full'  - always clamp magnitude to 1 (true vector normalization)
          'diag'  - normalize only when both axes active (classic fix)
          'off'   - no normalization (raw sum)
    -> stick deadzone (circular)
    -> XInput 16-bit output via vgamepad (lazy import; falls back to a
       stub when ViGEm is unavailable so the UI stays testable)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

try:
    import vgamepad as vg
    _HAS_VGAMEPAD = True
except Exception:  # pragma: no cover
    vg = None
    _HAS_VGAMEPAD = False


# ----------------------------------------------------------------- curves
def linear_curve(v: float) -> float:
    return v


# Safe evaluation sandbox: the ONLY builtins exposed to a curve expression
# are math functions + abs/min/max/len.  No imports, no globals, no dunder.
_SAFE_BUILTINS = {
    name: getattr(math, name)
    for name in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
                 "exp", "log", "log10", "log2", "pow", "floor", "ceil",
                 "cbrt", "fabs", "hypot", "asinh", "acosh", "tanh")
}
_SAFE_BUILTINS.update({
    "pi": math.pi, "e": math.e,
    "abs": abs, "min": min, "max": max, "len": len,
})


def compile_curve(expr: str) -> "callable | None":
    """Compile a response-curve expression into f(v)->float, or None.

    Allowed syntax: a Python arithmetic expression in the single variable
    `v` (0..1 input), using math functions (sqrt, sin, exp, ...) and
    abs/min/max/len, plus pi/e constants.  Examples:
        v
        1 - (1 - v)**2
        v**2 * (3 - 2 * v)
        sqrt(v) * 0.6 + v * 0.4
    Compiled with no __builtins__ and only the safe whitelist.  Returns
    None for invalid expressions (callers keep the previous curve).
    """
    if not expr or not expr.strip():
        return None
    try:
        code = compile(expr.strip(), "<response-curve>", "eval")
        for const in code.co_consts:
            if isinstance(const, str) and const.startswith("__"):
                return None
        allowed = {"v", "pi", "e"}
        for name in code.co_names:
            if name not in _SAFE_BUILTINS and name not in allowed:
                return None
        ns = {"v": 0.0, "pi": math.pi, "e": math.e,
              "__builtins__": _SAFE_BUILTINS}
        ns.update(_SAFE_BUILTINS)
        # trial runs: must produce finite floats across the domain
        for probe in (0.0, 0.1, 0.5, 0.9, 1.0):
            ns["v"] = probe
            out = eval(code, ns)  # noqa: S307 - whitelisted sandbox
            if not isinstance(out, (int, float)) or not math.isfinite(float(out)):
                return None
        return lambda v: float(eval(code, {**ns, "v": max(0.0, min(1.0, v))}))
    except Exception:
        return None


def curve_apply(v: float, fn: "callable | None") -> float:
    """Apply a compiled response curve (clamped to [0,1])."""
    if v <= 0.0:
        return 0.0
    if fn is None:
        return v
    try:
        out = fn(v)
        if not isinstance(out, (int, float)) or not math.isfinite(float(out)):
            return v
        return max(0.0, min(1.0, float(out)))
    except Exception:
        return v


# ----------------------------------------------------------------- config
@dataclass
class GamepadConfig:
    # --- sources ---
    normalize_mode: str = "full"          # full | diag | off
    # --- stick mapping ---
    stick: str = "left"                   # left | right
    invert_x: bool = False
    invert_y: bool = False
    # --- per-key shaping (key -> (deadzone 0..0.5, gain 0..3, curve)) ---
    deadzone: float = 0.02
    gain: float = 1.0
    curve: str = "linear"        # preset label (UI convenience)
    curve_expr: str = "v"        # dynamic response curve expression in v
    # --- stick-level ---
    stick_deadzone: float = 0.03          # circular deadzone 0..0.5
    max_update_hz: int = 500              # throttle pad.update()
    trigger_w: bool = False               # map W/S travel to LT/RT (0..255)
    block_y_on_trigger: bool = False      # with trigger_w: W/S only pull the
                                          # triggers — no vertical stick move
    digital_fallback: bool = False        # if analog ~0, still send digital dir
    live_autocal: bool = True             # self-calibrate key mapping on use

    def shaping_for(self, key: str):
        return self.deadzone, self.gain, self.curve


# ----------------------------------------------------------------- math
def apply_deadzone(v: float, dz: float) -> float:
    """Linear deadzone: |v| <= dz -> 0, else rescale to [0,1]."""
    s = 1.0 - dz
    if s <= 0.0:
        s = 0.5
    if abs(v) <= dz:
        return 0.0
    return (v - dz) / s if v > 0 else (v + dz) / s


def compose_and_normalize(vw: float, va: float, vs: float, vd: float,
                          cfg: GamepadConfig) -> tuple[float, float]:
    """Compose (x,y) from W/A/S/D values and normalize.

    x: + = D, - = A        y: + = W, - = S
    """
    x = vd - va
    y = vw - vs

    # gain is applied upstream per-key; here we just normalize.
    if cfg.normalize_mode == "off":
        # still hard-clamp each axis to [-1, 1] so we never exceed the stick
        return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))

    m = math.hypot(x, y)
    if m <= 1.0:
        return x, y
    # both axes exceed the unit circle: shrink to the circle.
    # In 'diag' mode we only do this when BOTH keys contribute; if only
    # one axis is active (straight movement) we just clamp that axis.
    if cfg.normalize_mode == "diag" and ((x == 0.0) or (y == 0.0)):
        return max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))
    inv = 1.0 / m
    return x * inv, y * inv


def stick_deadzone(x: float, y: float, dz: float) -> tuple[float, float]:
    if dz <= 0.0:
        return x, y
    m = math.hypot(x, y)
    if m <= dz:
        return 0.0, 0.0
    s = (m - dz) / (1.0 - dz)
    if s >= 1.0:
        return x, y
    return x * s, y * s


# ----------------------------------------------------------------- pad
class GamepadBridge:
    """Owns the virtual X360 pad and applies config each tick.

    If ViGEm is unavailable (driver missing) the bridge runs in *dry-run*
    mode: it computes everything and stores the intended 16-bit values in
    `last_x`/`last_y` so the GUI can display them, but no virtual device is
    registered.
    """

    def __init__(self, cfg: GamepadConfig):
        self.cfg = cfg
        self.pad = None
        self.dry_run = not _HAS_VGAMEPAD
        self.last_x = 0
        self.last_y = 0
        self.last_triggers = (0, 0)
        self.ticks = 0
        self._last_update = 0.0
        self._prev_xy = (0, 0)
        self._curve_fn = compile_curve(cfg.curve_expr) or linear_curve
        self._curve_src = cfg.curve_expr

    def set_curve(self, expr: str) -> bool:
        """Update the dynamic response curve from a new expression.

        Returns True on success; on invalid expressions the previous curve
        is kept (the UI shows the error via curve_error).
        """
        self._curve_src = expr
        fn = compile_curve(expr)
        if fn is None:
            return False
        self._curve_fn = fn
        return True

    def sample_curve(self, v: float) -> float:
        """For GUI preview: evaluate the current curve at v (0..1)."""
        return curve_apply(v, self._curve_fn)

    def open(self) -> tuple[bool, str]:
        if self.dry_run:
            return True, "dry-run (ViGEm unavailable — no virtual pad registered)"
        try:
            self.pad = vg.VX360Gamepad()
            return True, "virtual X360 pad registered"
        except Exception as e:
            self.dry_run = True
            return False, f"ViGEm registration failed: {e}"

    def close(self) -> None:
        if self.pad is not None:
            try:
                self.pad.reset()
                self.pad.update()
            except Exception:
                pass
            self.pad = None
            time.sleep(0.05)
            self.pad = None  # drop reference so __del__ unregisters

    def update(self, vw: float, va: float, vs: float, vd: float) -> tuple[int, int]:
        """Feed raw 0..1 travels; returns intended (x16, y16) 16-bit values."""
        cfg = self.cfg
        # per-key shaping: deadzone -> gain -> dynamic curve
        def shp(v: float) -> float:
            if v <= 0.0:
                return 0.0
            v = apply_deadzone(v, cfg.deadzone)
            v = v * cfg.gain
            v = curve_apply(v, self._curve_fn)
            return max(0.0, min(1.0, v))

        vw, va, vs, vd = shp(vw), shp(va), shp(vs), shp(vd)

        x, y = compose_and_normalize(vw, va, vs, vd, cfg)
        x, y = stick_deadzone(x, y, cfg.stick_deadzone)

        if cfg.invert_x:
            x = -x
        if cfg.invert_y:
            y = -y

        # option: while driving the triggers with W/S, suppress the vertical
        # stick so W/S act ONLY as LT/RT (the trigger, not the stick, is the
        # "drive" channel in this mode).
        if cfg.trigger_w and cfg.block_y_on_trigger:
            y = 0.0

        # digital fallback: if everything is ~zero, no stick input.
        # The triggers are computed INDEPENDENTLY of the stick (in
        # triggers-only mode the stick can be centred while W/S pull LT/RT).
        if abs(x) < 1e-6 and abs(y) < 1e-6 and not cfg.digital_fallback:
            x16 = y16 = 0
        else:
            x16 = int(round(x * 32767))
            y16 = int(round(y * 32767))
            x16 = max(-32767, min(32767, x16))
            y16 = max(-32767, min(32767, y16))
        lt = int(round(vw * 255)) if cfg.trigger_w else 0
        rt = int(round(vs * 255)) if cfg.trigger_w else 0

        now = time.monotonic()
        min_interval = 1.0 / max(1, cfg.max_update_hz)
        changed = (x16, y16, lt, rt) != (self.last_x, self.last_y, *self.last_triggers)
        if self.dry_run or now - self._last_update >= min_interval or changed:
            self._last_update = now
            self.ticks += 1
            if not self.dry_run and self.pad is not None:
                if cfg.stick == "left":
                    self.pad.left_joystick(x16, y16)
                else:
                    self.pad.right_joystick(x16, y16)
                if cfg.trigger_w:
                    self.pad.left_trigger(lt)
                    self.pad.right_trigger(rt)
                self.pad.update()
        self.last_x = x16
        self.last_y = y16
        self.last_triggers = (lt, rt)
        return x16, y16

    @property
    def magnitude(self) -> float:
        return math.hypot(self.last_x, self.last_y) / 32767.0
