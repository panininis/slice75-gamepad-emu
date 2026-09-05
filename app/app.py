"""Slice Pad — analog WASD -> virtual Xbox 360 gamepad for the
Chilkey Slice75 HE Hall Effect keyboard.

Dark-mode GUI (customtkinter) with:
  * live stick ball + 4 analog bars (W/A/S/D)
  * diagonal vector normalization modes (full / diagonal / off)
  * per-axis shaping: deadzone, gain, response curve
  * stick selection (left/right), axis inversion, stick deadzone
  * poll-rate / update-rate controls
  * interactive key calibration (press a key, it detects the source)
  * live telemetry: rate, frames, FW info, dry-run badge
  * start/stop engine, mapping persistence

Run:  python app.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import customtkinter as ctk
from tkinter import messagebox

# Ensure the app package (this dir) is importable regardless of CWD
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from slice_capture import (  # noqa: E402
    DigitalKeys, HidAnalog, Mapping, OsKeys, VendorStream,
    find_interfaces, read_key_value,
)
from gamepad_bridge import GamepadBridge, GamepadConfig  # noqa: E402
from applog import (  # noqa: E402
    log, log_exception, install_hooks, touch_latest, LOG_FILE,
)

APP_DIR = os.path.join(os.path.expanduser("~"), ".slice-pad")
MAPPING_PATH = os.path.join(APP_DIR, "mapping.json")
CFG_PATH = os.path.join(APP_DIR, "config.json")

# Dynamic response-curve presets: (label, expression in v)
CURVE_PRESETS = [
    ("linear", "v"),
    ("squared", "v**2"),
    ("cubic", "v**3"),
    ("sqrt", "sqrt(v)"),
    ("inverse sqrt", "1 - sqrt(1 - v)"),
    ("smooth step", "v*v*(3 - 2*v)"),
    ("aggressive", "min(1, v/0.6)"),
    ("S-curve", "0.5 + 0.5*sin(pi*(v - 0.5))"),
    ("center boost", "v + 0.15*sin(pi*v)"),
    ("dead-center push", "max(0, (v - 0.08)/0.92)**1.5"),
]

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ---------------- theme (vibecoded dark palette) ----------------
BG = "#07090d"        # app background — near-black
PANEL = "#0e1420"     # glass cards
PANEL2 = "#0a0f18"    # inset wells
CARD = PANEL          # legacy alias (kept for canvas surfaces)
CARD2 = "#131a28"
ACCENT = "#8b5cf6"    # violet
ACCENT_HOVER = "#7c3aed"
ACCENT2 = "#22d3ee"   # cyan
ACCENT2_HOVER = "#0ea5b7"
GOOD = "#34d399"
WARN = "#fbbf24"
BAD = "#fb7185"
BAD_HOVER = "#e11d48"
TEXT = "#e8edf4"
DIM = "#8b95a7"
FAINT = "#5b6474"
TRACK = "#1a2333"
BORDER = "#1e2939"    # 1px glass border


class StickCanvas(ctk.CTkFrame):
    """Renders the virtual stick on a glass pad: concentric rings, WASD
    keycap chips, a glowing normalized ball, and a dashed raw
    (pre-normalization) ghost so diagonal correction is visible."""

    def __init__(self, master, size=252, **kw):
        super().__init__(master, fg_color=PANEL, corner_radius=18,
                         border_width=1, border_color=BORDER, **kw)
        self.size = size
        self.canvas = ctk.CTkCanvas(self, width=size, height=size,
                                    bg=PANEL, highlightthickness=0)
        self.canvas.pack(padx=12, pady=12, expand=True)
        self._draw_base()
        self.x = 0.0
        self.y = 0.0
        self.gx = 0.0
        self.gy = 0.0
        self.after(33, self._render)

    def _keycap(self, x, y, ch, hot):
        c = self.canvas
        c.create_rectangle(x - 13, y - 13, x + 13, y + 13, fill=PANEL2,
                           outline=BORDER, width=1, tags="ball")
        c.create_text(x, y, text=ch,
                      fill=ACCENT2 if hot else FAINT,
                      font=("Segoe UI", 12, "bold"), tags="ball")

    def _draw_base(self):
        c = self.canvas
        s = self.size
        cx = cy = s / 2
        r = s / 2 - 18
        for f in (1.0, 0.66, 0.33):
            rr = r * f
            col = BORDER if f == 1.0 else "#182234"
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                          outline=col, width=1, tags="base")
        c.create_line(cx, cy - r, cx, cy + r, fill="#151d2b", tags="base")
        c.create_line(cx - r, cy, cx + r, cy, fill="#151d2b", tags="base")
        c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=FAINT, tags="base")

    def set(self, x, y, gx, gy):
        self.x, self.y, self.gx, self.gy = x, y, gx, gy

    def _render(self):
        c = self.canvas
        c.delete("ball")
        s = self.size
        cx = cy = s / 2
        r = s / 2 - 18
        # ghost (raw, pre-normalization) — dashed hollow ring
        if abs(self.gx) > 0.02 or abs(self.gy) > 0.02:
            gx = cx + max(-1.0, min(1.0, self.gx)) * r
            gy = cy - max(-1.0, min(1.0, self.gy)) * r
            c.create_oval(gx - 9, gy - 9, gx + 9, gy + 9,
                          outline="#46536b", width=1.5, dash=(3, 3), tags="ball")
        # normalized ball with layered glow
        bx = cx + self.x * r
        by = cy - self.y * r
        c.create_oval(bx - 16, by - 16, bx + 16, by + 16,
                      outline="#134b5e", width=3, tags="ball")
        c.create_oval(bx - 12, by - 12, bx + 12, by + 12,
                      outline="#0e6478", width=2, tags="ball")
        c.create_oval(bx - 9, by - 9, bx + 9, by + 9,
                      fill=ACCENT, outline=ACCENT2, width=2, tags="ball")
        c.create_oval(bx - 5, by - 5, bx - 1, by - 1, fill="#dbeafe", tags="ball")
        # keycap chips light up while their axis drives the stick
        self._keycap(cx, 15, "W", self.y > 0.08)
        self._keycap(cx, s - 15, "S", self.y < -0.08)
        self._keycap(15, cy, "A", self.x < -0.08)
        self._keycap(s - 15, cy, "D", self.x > 0.08)
        self.after(33, self._render)


class AnalogBar(ctk.CTkFrame):
    def __init__(self, master, key, **kw):
        super().__init__(master, fg_color=PANEL, corner_radius=12,
                         border_width=1, border_color=BORDER, **kw)
        self.key = key
        cap = ctk.CTkFrame(self, width=30, height=30, fg_color=PANEL2,
                           corner_radius=8, border_width=1, border_color=BORDER)
        cap.grid(row=0, column=0, rowspan=2, sticky="n", padx=(10, 8), pady=(9, 9))
        ctk.CTkLabel(cap, text=key, font=("Segoe UI", 13, "bold"),
                     text_color=ACCENT2).pack(expand=True)
        self.value = ctk.CTkLabel(self, text="0", font=("Consolas", 12, "bold"),
                                  text_color=DIM, width=44, anchor="e")
        self.value.grid(row=0, column=2, sticky="e", padx=(4, 10), pady=(9, 0))
        self.track = ctk.CTkFrame(self, height=8, fg_color=TRACK, corner_radius=4)
        self.track.grid(row=1, column=0, columnspan=3, sticky="ew",
                        padx=10, pady=(6, 10))
        self.fill = ctk.CTkFrame(self.track, height=8, fg_color=ACCENT2, corner_radius=4)
        self.fill.place(relx=0.0, rely=0.0, relheight=1.0, relwidth=0.0)
        self.set(0.0)

    def set(self, v):
        v = max(0.0, min(1.0, v))
        self.value.configure(text=f"{v * 100:3.0f}",
                             text_color=TEXT if v > 0.02 else DIM)
        self.fill.place_configure(relwidth=0.0 if v <= 0.005 else min(1.0, v))
        # cyan while moving, violet at full travel
        self.fill.configure(fg_color=ACCENT if v > 0.85 else ACCENT2)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Slice Pad  ·  WASD → Xbox 360")
        # Frameless window: the title bar is drawn INSIDE the client area
        # (custom buttons + drag), with acrylic blur + slight translucency
        # applied post-map in _apply_chrome().
        self.overrideredirect(True)
        self.configure(fg_color=BG)
        W, H = 980, 580
        x = max(0, (self.winfo_screenwidth() - W) // 2)
        y = max(0, (self.winfo_screenheight() - H) // 3)
        self.geometry(f"{W}x{H}+{x}+{y}")
        self._maximized = False
        self._normal_geom = None
        self._drag_dx = 0
        self._drag_dy = 0
        self.after(30, self._apply_chrome)
        self.after(90, self._force_focus)

        self.vendor = None
        self.hid = None
        self.dkeys = None
        self.oskeys = OsKeys()      # OS-level WASD press watcher (win32)
        self.bridge = None
        self._calib_running = False
        self.mapping = Mapping.load(MAPPING_PATH)
        self.cfg = self._load_config()
        self.running = False
        self.engine = None
        self._stop_evt = threading.Event()
        self._ui_queue: list = []
        self._ui_qlock = threading.Lock()      # queue is touched by engine + UI threads
        self._ui_dirty = threading.Event()
        self.stats = {"frames": 0, "rate": 0.0, "vendor_frames": 0, "hz": 0.0}
        log("app starting", "INFO")
        log(f"python {sys.version.split()[0]}  |  log file: {LOG_FILE}")
        self._key_states = {"W": 0.0, "A": 0.0, "S": 0.0, "D": 0.0}
        self._curve_error = ""
        self._cal_key = None
        self._lc: dict = {}  # live auto-cal tracking: key -> state
        # pre-press baseline per key: {key: {"hid": [6], "ven": {pos: mm}, "t": ts}}
        # refreshed by the engine loop while the key is NOT held — calibration
        # scores *rise from baseline*, so stale values (e.g. a previous key's
        # travel that was never zeroed) can never win the mapping.
        self._key_baseline: dict = {}

        self._build_ui()
        self.after(100, self._poll_ui)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- config persistence ----------------
    def _load_config(self) -> GamepadConfig:
        cfg = GamepadConfig()
        if os.path.exists(CFG_PATH):
            try:
                with open(CFG_PATH) as f:
                    d = json.load(f)
                for k, v in d.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        return cfg

    def _save_config(self):
        os.makedirs(APP_DIR, exist_ok=True)
        d = {}
        for k in ("normalize_mode", "stick", "invert_x", "invert_y", "deadzone",
                  "gain", "curve", "curve_expr", "stick_deadzone",
                  "max_update_hz", "trigger_w", "digital_fallback"):
            d[k] = getattr(self.cfg, k)
        with open(CFG_PATH, "w") as f:
            json.dump(d, f, indent=2)

    # ---------------- UI ----------------
    def _section(self, parent, title, icon):
        """Glass panel with a dim small-caps section label."""
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=14,
                            border_width=1, border_color=BORDER)
        ctk.CTkLabel(card, text=f"{icon}   {title}", font=("Segoe UI", 11, "bold"),
                     text_color=FAINT, anchor="w").grid(row=0, column=0, columnspan=2,
                                                        sticky="w", padx=15, pady=(11, 2))
        return card

    def _build_ui(self):
        root = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        root.pack(fill="both", expand=True)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)

        # ---- custom in-window title bar (frameless window) ----
        tbar = ctk.CTkFrame(root, fg_color="transparent", height=42)
        tbar.grid(row=0, column=0, sticky="ew")
        logo = ctk.CTkLabel(tbar, text="◆", font=("Segoe UI", 14, "bold"),
                            text_color=ACCENT2)
        logo.pack(side="left", padx=(14, 6))
        name = ctk.CTkLabel(tbar, text="Slice Pad", font=("Segoe UI", 12, "bold"),
                            text_color=DIM)
        name.pack(side="left")
        self.status_pill = ctk.CTkFrame(tbar, fg_color=PANEL2, corner_radius=999,
                                        border_width=1, border_color=BORDER)
        self.status_pill.pack(side="left", padx=14)
        self.status_lbl = ctk.CTkLabel(self.status_pill, text="●  idle",
                                       font=("Segoe UI", 11, "bold"), text_color=DIM,
                                       padx=10, pady=4)
        self.status_lbl.pack()
        self.close_btn = ctk.CTkButton(tbar, text="✕", width=30, height=24,
                                       corner_radius=6, fg_color="transparent",
                                       hover_color="#3b1424", text_color=DIM,
                                       font=("Segoe UI", 11, "bold"),
                                       command=self._on_close)
        self.close_btn.pack(side="right", padx=(2, 10))
        self.max_btn = ctk.CTkButton(tbar, text="□", width=30, height=24,
                                     corner_radius=6, fg_color="transparent",
                                     hover_color="#1b2436", text_color=FAINT,
                                     font=("Segoe UI", 9, "bold"),
                                     command=self._toggle_max)
        self.max_btn.pack(side="right", padx=2)
        self.min_btn = ctk.CTkButton(tbar, text="—", width=30, height=24,
                                     corner_radius=6, fg_color="transparent",
                                     hover_color="#1b2436", text_color=FAINT,
                                     font=("Segoe UI", 13, "bold"),
                                     command=self._minimize)
        self.min_btn.pack(side="right", padx=2)
        for w in (tbar, logo, name, self.status_pill):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        # cyan → violet accent line under the title bar
        grad = ctk.CTkFrame(root, fg_color="transparent", height=2)
        grad.grid(row=1, column=0, sticky="ew")
        ctk.CTkFrame(grad, fg_color=ACCENT2, height=2).pack(side="left", fill="x",
                                                           expand=True)
        ctk.CTkFrame(grad, fg_color=ACCENT, height=2).pack(side="left", fill="x",
                                                          expand=True)

        body = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
        body.grid(row=2, column=0, sticky="nsew", padx=12, pady=(8, 12))
        body.grid_columnconfigure(0, weight=5)
        body.grid_columnconfigure(1, weight=7)
        body.grid_rowconfigure(0, weight=1)

        # ---------- left column: stick + bars ----------
        left = ctk.CTkFrame(body, fg_color=BG, corner_radius=0)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_rowconfigure(0, weight=1)

        self.stick = StickCanvas(left, size=240)
        self.stick.grid(row=0, column=0, sticky="nsew")

        bars_frame = ctk.CTkFrame(left, fg_color=BG, corner_radius=0)
        bars_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.bars = {}
        for i, k in enumerate("WASD"):
            b = AnalogBar(bars_frame, k)
            b.grid(row=i, column=0, sticky="ew", pady=3)
            b.grid_columnconfigure(1, weight=1)
            self.bars[k] = b
        self.mag_lbl = ctk.CTkLabel(left, text="magnitude 0.00  ·  ghost raw",
                                    font=("Consolas", 11), text_color=DIM, anchor="w")
        self.mag_lbl.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        # ---------- right column: controls ----------
        right = ctk.CTkScrollableFrame(body, fg_color=BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)

        # engine card
        eng = self._section(right, "ENGINE", "⚡")
        eng.grid(sticky="ew", pady=(0, 8), padx=2)
        eng.grid_columnconfigure((0, 1, 2), weight=1)
        self.engine_lbl = ctk.CTkLabel(eng, text="disconnected", font=("Segoe UI", 13, "bold"),
                                       text_color=FAINT, anchor="w")
        self.engine_lbl.grid(row=1, column=0, columnspan=3, sticky="w", padx=15, pady=(0, 8))

        self.start_btn = ctk.CTkButton(eng, text="▶   Connect & Start", height=40,
                                       corner_radius=10,
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       font=("Segoe UI", 13, "bold"),
                                       command=self.toggle_engine)
        self.start_btn.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 12))

        # calibration card
        calib = self._section(right, "KEY CALIBRATION", "⚙")
        calib.grid(sticky="ew", pady=8, padx=2)
        calib.grid_columnconfigure(1, weight=1)
        self.calib_lbl = ctk.CTkLabel(calib, text="Maps W/A/S/D to their analog "
                                                  "sources (vendor matrix / HID byte).",
                                      text_color=DIM, font=("Segoe UI", 11), anchor="w",
                                      justify="left")
        self.calib_lbl.grid(row=1, column=0, columnspan=2, sticky="w", padx=15, pady=(0, 8))
        calib_btns = ctk.CTkFrame(calib, fg_color=PANEL2, corner_radius=8,
                                  border_width=1, border_color=BORDER)
        calib_btns.grid(row=2, column=0, columnspan=2, sticky="ew", padx=15, pady=(0, 12))
        calib_btns.grid_columnconfigure((0, 1), weight=1)
        self.calib_btn = ctk.CTkButton(calib_btns, text="⚙  Calibrate now", height=34,
                                       corner_radius=8,
                                       fg_color=ACCENT2, hover_color=ACCENT2_HOVER,
                                       text_color="#062a33",
                                       font=("Segoe UI", 12, "bold"),
                                       command=self._calibrate_start)
        self.calib_btn.grid(row=0, column=0, sticky="ew", padx=(6, 3), pady=6)
        self.calib_auto_lbl = ctk.CTkLabel(calib_btns, text="auto-detect on run",
                                           font=("Segoe UI", 11), text_color=DIM,
                                           anchor="e")
        self.calib_auto_lbl.grid(row=0, column=1, sticky="ew", padx=(3, 8), pady=6)

        # normalization card
        norm = self._section(right, "DIAGONAL NORMALIZATION", "◧")
        norm.grid(sticky="ew", pady=8, padx=2)
        norm.grid_columnconfigure(1, weight=1)
        self.norm_var = ctk.StringVar(value=self.cfg.normalize_mode)
        self.norm_seg = ctk.CTkSegmentedButton(norm, values=["Full vector", "Diagonal only", "Off"],
                                               width=280)
        self.norm_seg.set(self._norm_label(self.cfg.normalize_mode))
        self.norm_seg.grid(row=1, column=0, columnspan=2, sticky="w", padx=15, pady=(2, 6))
        self.norm_seg.configure(command=lambda lab: self._set_cfg("normalize_mode",
                                                                  self._norm_value(lab)))
        ctk.CTkLabel(norm, text="Full: clamp √(x²+y²) ≤ 1 — diagonal never exceeds straight.",
                     text_color=DIM, font=("Segoe UI", 11), anchor="w",
                     justify="left").grid(row=2, column=0, columnspan=2, sticky="w",
                                          padx=14, pady=(0, 10))

        # shaping card
        shape = self._section(right, "PER-KEY SHAPING", "◔")
        shape.grid(sticky="ew", pady=8, padx=2)
        shape.grid_columnconfigure(1, weight=1)
        self._slider(shape, 1, "Deadzone", 0.0, 0.5, self.cfg.deadzone, "deadzone", 2)
        self._slider(shape, 2, "Gain", 0.1, 3.0, self.cfg.gain, "gain", 2)
        # dynamic response curve
        ctk.CTkLabel(shape, text="Response curve  (f(v), v = 0..1 travel)",
                     font=("Segoe UI", 12), text_color=TEXT, anchor="w",
                     ).grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=(6, 0))
        self._build_curve_card(shape, 4)

        # stick card
        stick = self._section(right, "STICK", "✛")
        stick.grid(sticky="ew", pady=8, padx=2)
        stick.grid_columnconfigure(1, weight=1)
        row = ctk.CTkFrame(stick, fg_color=PANEL2, corner_radius=8,
                           border_width=1, border_color=BORDER)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=15, pady=(0, 6))
        row.grid_columnconfigure((0, 1, 2), weight=1)
        self.stick_seg = ctk.CTkSegmentedButton(row, values=["Left stick", "Right stick"],
                                                width=220)
        self.stick_seg.set("Left stick" if self.cfg.stick == "left" else "Right stick")
        self.stick_seg.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=8)
        self.stick_seg.configure(command=lambda lab: self._set_cfg(
            "stick", "left" if lab == "Left stick" else "right"))
        self._slider(stick, 2, "Stick deadzone", 0.0, 0.5, self.cfg.stick_deadzone,
                     "stick_deadzone", 2)

        # toggles card
        tog = self._section(right, "OPTIONS", "⌗")
        tog.grid(sticky="ew", pady=8, padx=2)
        tog.grid_columnconfigure(1, weight=1)
        self._toggle(tog, 1, "Invert X axis", "invert_x")
        self._toggle(tog, 2, "Invert Y axis", "invert_y")
        self._toggle(tog, 3, "Drive triggers with W/S (0–255)", "trigger_w")
        self._slider(tog, 4, "Max update rate (Hz)", 30, 1000, self.cfg.max_update_hz,
                     "max_update_hz", 0)

        # telemetry card
        tele = self._section(right, "TELEMETRY", "▤")
        tele.grid(sticky="ew", pady=(8, 2), padx=2)
        tele.grid_columnconfigure(0, weight=1)
        self.tele_lbl = ctk.CTkLabel(tele, text="—", font=("Consolas", 11),
                                     text_color=DIM, anchor="w", justify="left")
        self.tele_lbl.grid(row=1, column=0, sticky="w", padx=15, pady=(0, 12))

    # ---------------- dynamic response curve ----------------
    def _build_curve_card(self, parent, row):
        box = ctk.CTkFrame(parent, fg_color=BG, corner_radius=8)
        box.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 6))
        top = ctk.CTkFrame(box, fg_color=BG, corner_radius=0)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.curve_var = ctk.StringVar(value=getattr(self.cfg, "curve_expr", "v"))
        self.curve_preset = ctk.CTkOptionMenu(
            top, values=[n for n, _ in CURVE_PRESETS], width=150,
            command=self._on_curve_preset)
        self.curve_preset.set(self.cfg.curve if self.cfg.curve in
                              [n for n, _ in CURVE_PRESETS] else "linear")
        self.curve_preset.pack(side="left")
        self.curve_entry = ctk.CTkEntry(top, textvariable=self.curve_var, width=330,
                                        font=("Consolas", 12))
        self.curve_entry.pack(side="left", padx=8)
        self.curve_entry.bind("<Return>", lambda e: self._on_curve_submit())
        ctk.CTkButton(top, text="Apply", width=64, corner_radius=8, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=self._on_curve_submit
                      ).pack(side="left")
        self._curve_lbl_err = ctk.CTkLabel(box, text="", font=("Segoe UI", 11),
                                           text_color=DIM, anchor="w")
        self._curve_lbl_err.pack(fill="x", padx=10)
        self.curve_canvas = ctk.CTkCanvas(box, width=420, height=110, bg=PANEL,
                                          highlightthickness=0)
        self.curve_canvas.pack(fill="x", padx=10, pady=(2, 8))
        self._redraw_curve()

    def _on_curve_preset(self, name):
        for label, expr in CURVE_PRESETS:
            if label == name:
                self.curve_var.set(expr)
                break
        self.cfg.curve = name
        self._on_curve_submit()

    def _on_curve_submit(self):
        expr = (self.curve_var.get() or "").strip()
        if not expr:
            self._curve_error = "empty expression"
            self._curve_lbl_err.configure(text=self._curve_error, text_color=BAD)
            return
        self._set_cfg("curve_expr", expr)

    def _redraw_curve(self):
        c = self.curve_canvas
        c.delete("all")
        W, H = 420, 110
        x0, y0, x1, y1 = 10, 8, W - 10, H - 8
        c.create_rectangle(x0, y0, x1, y1, outline=BORDER, fill=PANEL2)
        for g in (0.0, 0.25, 0.5, 0.75, 1.0):
            gx = x0 + (x1 - x0) * g
            c.create_line(gx, y0, gx, y1, fill="#151d2b")
            gy = y1 - (y1 - y0) * g
            c.create_line(x0, gy, x1, gy, fill="#151d2b")
        c.create_line(x0, y1, x1, y0, fill="#3b475c", dash=(4, 4))  # identity
        fn = self.bridge.sample_curve if self.bridge is not None else None
        if fn is None:
            from gamepad_bridge import compile_curve
            fn = compile_curve(getattr(self.cfg, "curve_expr", "v"))
        n = 90
        pts: list[float] = []
        for i in range(n + 1):
            v = i / n
            o = fn(v) if fn is not None else v
            o = max(0.0, min(1.0, o))
            pts.append(x0 + (x1 - x0) * v)
            pts.append(y1 - (y1 - y0) * o)
        c.create_line(*pts, fill=ACCENT2, width=2, smooth=True)
        c.create_text(x0, y1 + 4, text="0", fill=DIM, font=("Segoe UI", 9))
        c.create_text(x1, y1 + 4, text="1.0 (travel)", fill=DIM, font=("Segoe UI", 9))
        c.create_text(x0 - 6, y0, text="1.0", fill=DIM, font=("Segoe UI", 9))

    def _norm_label(self, v):
        return {"full": "Full vector", "diag": "Diagonal only", "off": "Off"}[v]

    def _norm_value(self, label):
        return {"Full vector": "full", "Diagonal only": "diag", "Off": "off"}[label]

    def _slider(self, parent, row, label, lo, hi, value, attr, decimals):
        ctk.CTkLabel(parent, text=label, font=("Segoe UI", 12),
                     text_color=TEXT).grid(row=row, column=0, sticky="w",
                                           padx=15, pady=(6, 0))
        box = ctk.CTkFrame(parent, fg_color=PANEL2, corner_radius=8,
                           border_width=1, border_color=BORDER)
        box.grid(row=row, column=1, sticky="ew", padx=15, pady=(6, 6))
        box.grid_columnconfigure(0, weight=1)
        val = ctk.CTkLabel(box, text=f"{value:.{decimals}f}", font=("Consolas", 12, "bold"),
                           text_color=ACCENT2, width=44, anchor="e")
        val.grid(row=0, column=1, sticky="e", padx=(0, 8), pady=(4, 4))
        s = ctk.CTkSlider(box, from_=lo, to=hi, number_of_steps=int((hi - lo) * 100),
                          fg_color=TRACK, progress_color=ACCENT, button_color=ACCENT2,
                          button_hover_color=TEXT, width=260)
        s.set(value)
        s.grid(row=0, column=0, sticky="w", padx=8, pady=(4, 4))
        s.configure(command=lambda v: self._on_slider(attr, decimals, float(v), val))
        return s

    def _on_slider(self, attr, decimals, v, val):
        val.configure(text=f"{v:.{decimals}f}")
        if attr in ("deadzone", "gain", "stick_deadzone"):
            self._set_cfg(attr, round(v, 3))
        elif attr == "max_update_hz":
            self._set_cfg("max_update_hz", int(round(v)))

    def _toggle(self, parent, row, label, attr):
        def cb(v):
            self._set_cfg(attr, bool(v))
        var = ctk.BooleanVar(value=bool(getattr(self.cfg, attr)))
        ctk.CTkCheckBox(parent, text=label, font=("Segoe UI", 12), text_color=TEXT,
                        variable=var, command=lambda: cb(var.get()),
                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        checkmark_color="#f5f3ff", border_color=BORDER
                        ).grid(row=row, column=0, columnspan=2, sticky="w",
                               padx=15, pady=4)

    def _set_cfg(self, attr, v):
        setattr(self.cfg, attr, v)
        if attr in ("curve", "curve_expr") and self.bridge is not None:
            self._apply_curve()
        self._save_config()

    def _apply_curve(self):
        """Recompile the dynamic curve into the bridge (UI thread)."""
        if self.bridge is None:
            return
        expr = getattr(self.cfg, "curve_expr", "v")
        ok = self.bridge.set_curve(expr)
        if ok:
            self._curve_error = ""
        else:
            self._curve_error = "invalid expression — previous curve kept"
        self._curve_lbl_err.configure(text=self._curve_error,
                                      text_color=BAD if self._curve_error else DIM)
        self._redraw_curve()
        log(f"response curve {'updated' if ok else 'REJECTED (invalid)'}: {expr!r}")

    def _key_locked(self, key: str) -> bool:
        src = self.mapping.keys.get(key)
        if src == "adc":
            return key in self.mapping.adc_pos
        if src == "vendor":
            return key in self.mapping.vendor_pos
        if src == "hid":
            return key in self.mapping.hid_byte
        return False

    def _refresh_baselines(self, held: set):
        """Per-key pre-press baselines for calibration (engine thread).
        Updated only while the key is NOT held, at most every 250 ms."""
        now = time.time()
        due = []
        for k in ("W", "A", "S", "D"):
            if k in held:
                continue
            b = self._key_baseline.get(k)
            if b is None or now - b["t"] >= 0.25:
                due.append(k)
        if not due:
            return
        hid_snap = self.hid.snapshot() if (self.hid and self.hid.opened) else None
        ven_snap = self.vendor.snapshot() if (self.vendor and self.vendor.running) else None
        adc_snap = self.vendor.travel_snapshot() if (self.vendor and self.vendor.running) else None
        for k in due:
            self._key_baseline[k] = {"hid": hid_snap or [0] * 6,
                                     "ven": ven_snap or {},
                                     "adc": adc_snap or {}, "t": now}

    def _live_autocal(self, wasd_held: set):
        """Self-calibration: while exactly one WASD key is held, watch which
        channel spikes; after a short hold with a strong spike, lock the
        mapping and persist it.  Works for both data sources.
        Runs in the ENGINE thread — must only use thread-safe snapshots."""
        if len(wasd_held) != 1 or not self.cfg.live_autocal:
            return
        k = next(iter(wasd_held))
        if self._key_locked(k):
            return
        st = self._lc.get(k)
        if st is None:
            st = self._lc[k] = {"t0": time.time(), "hid": [0] * 6, "ven": {},
                                "adc": {}, "done": False}
            base = self._key_baseline.get(k)
            st["base_hid"] = base["hid"] if base else [0] * 6
            st["base_ven"] = base["ven"] if base else {}
        now = time.time()
        # sample peaks (thread-safe snapshots)
        hid_snap = self.hid.snapshot() if (self.hid and self.hid.opened) else None
        if hid_snap:
            for i, v in enumerate(hid_snap):
                if v > st["hid"][i]:
                    st["hid"][i] = v
        ven_snap = self.vendor.snapshot() if (self.vendor and self.vendor.running) else None
        if ven_snap:
            for pos, v in ven_snap.items():
                if v > st["ven"].get(pos, 0):
                    st["ven"][pos] = v
        adc_snap = self.vendor.travel_snapshot() if (self.vendor and self.vendor.running) else None
        if adc_snap:
            for pos, v in adc_snap.items():
                if v > st["adc"].get(pos, 0.0):
                    st["adc"][pos] = v
        if now - st["t0"] < 0.35:
            return
        # ADC travel is already 0..1 (0 at rest, ~1 at full press): score the
        # peak directly — no baseline subtraction needed, and it is immune to
        # slow rest drift.  Legacy sources still use rise-from-baseline.
        hid_rise = [max(0, st["hid"][i] - st["base_hid"][i]) for i in range(6)]
        ven_rise = {p: max(0, v - st["base_ven"].get(p, 0)) for p, v in st["ven"].items()}
        hid_best = max(hid_rise)
        ven_best = max(ven_rise.values()) if ven_rise else 0
        adc_best = max(st["adc"].values()) if st["adc"] else 0.0
        hid_i = hid_rise.index(hid_best)
        ven_p = max(ven_rise, key=lambda q: ven_rise[q]) if ven_rise else None
        adc_p = max(st["adc"], key=lambda q: st["adc"][q]) if st["adc"] else None
        hid_score = hid_best / 255.0
        ven_score = ven_best / 3300.0
        lock_src = None
        if adc_best >= 0.15:
            lock_src = ("adc", adc_p, f"travel {adc_best:.2f}")
        elif ven_score >= 0.15 and ven_score >= hid_score * 0.8:
            lock_src = ("vendor", ven_p, f"rise {ven_best/1000:.3f} mm")
        elif hid_score >= 0.15:
            lock_src = ("hid", hid_i, f"rise {hid_best}/255")
        if lock_src is None:
            return
        src, ident, val = lock_src
        try:
            self.mapping.keys[k] = src
            if src == "adc":
                self.mapping.adc_pos[k] = ident
            elif src == "vendor":
                self.mapping.vendor_pos[k] = ident
            else:
                self.mapping.hid_byte[k] = ident
            self.mapping.save(MAPPING_PATH)
        except Exception as e:
            log_exception(f"live-autocal save {k}", e)
            return
        st["done"] = True
        log(f"auto-calibrated {k} → {src} {ident} (peak {val})")
        self._ui_queue_append(("notify", f"auto-calibrated {k} → {src} {ident}  (peak {val})", GOOD))

    # ---------------- calibration ----------------
    def _ui_notify(self, text, color=DIM):
        """UI-thread only. (Background threads use _ui_queue 'notify'.)"""
        self.calib_lbl.configure(text=text, text_color=color)

    def _ui_queue_append(self, item):
        with self._ui_qlock:
            self._ui_queue.append(item)
            if len(self._ui_queue) > 64:          # bound: drop oldest
                self._ui_queue.pop(0)
        self._ui_dirty.set()

    def _collect_spike(self, key: str, t0: float, dur: float = 2.5):
        """Collect which sources spike while key `key` is held.
        CALIBRATION THREAD: reads only via thread-safe snapshots.
        adc_max is the peak normalized travel (0..1) per sensor pos."""
        hid_max = [0] * 6
        ven_max = {}
        adc_max: dict[int, float] = {}
        t_end = t0 + dur
        n = 0
        while time.time() < t_end:
            n += 1
            if n % 500 == 0:
                log(f"calibrate {key}: collecting {n*0.002:.1f}s "
                    f"(hid max {max(hid_max)} ven max "
                    f"{max(ven_max.values(), default=0)} adc max "
                    f"{max(adc_max.values(), default=0):.2f})", "DEBUG")
            hid_snap = self.hid.snapshot() if (self.hid and self.hid.opened) else None
            if hid_snap:
                for i, v in enumerate(hid_snap):
                    if v > hid_max[i]:
                        hid_max[i] = v
            ven_snap = self.vendor.snapshot() if (self.vendor and self.vendor.running) else None
            if ven_snap:
                for p2, v in ven_snap.items():
                    if v > ven_max.get(p2, 0):
                        ven_max[p2] = v
            adc_snap = self.vendor.travel_snapshot() if (self.vendor and self.vendor.running) else None
            if adc_snap:
                for p2, v in adc_snap.items():
                    if v > adc_max.get(p2, 0.0):
                        adc_max[p2] = v
            time.sleep(0.002)
        ven_nz = {p2: v for p2, v in ven_max.items() if v > 50}
        adc_nz = {p2: round(v, 3) for p2, v in adc_max.items() if v > 0.05}
        log(f"calibrate {key}: peaks hid={hid_max} ven={ven_nz} adc={adc_nz}")
        return hid_max, ven_max, adc_max

    def _peek_baseline(self):
        """Thread-safe snapshot of current analog state (pre-press baseline)."""
        hid = self.hid.snapshot() if (self.hid and self.hid.opened) else [0] * 6
        ven = self.vendor.snapshot() if (self.vendor and self.vendor.running) else {}
        adc = self.vendor.travel_snapshot() if (self.vendor and self.vendor.running) else {}
        return hid, ven, adc

    def _pick_source(self, key: str, hid_rise, ven_rise, adc_peak=None):
        """Choose the best data source for `key` and record the mapping.

        adc_peak: {pos: travel 0..1} — raw Hall ADC (primary, self-normalized:
        0 at rest, ~1 at full press, so no baseline subtraction is needed).
        hid_rise / ven_rise are legacy RISES from the pre-press baseline.
        """
        if not self.mapping:
            self.mapping = Mapping.load(MAPPING_PATH)
        best_hid = max(range(6), key=lambda i: hid_rise[i])
        best_hid_v = hid_rise[best_hid]
        if ven_rise:
            best_ven_p = max(ven_rise, key=lambda p2: ven_rise[p2])
            best_ven_v = ven_rise[best_ven_p]
        else:
            best_ven_v = 0
        ven_score = best_ven_v / 3300.0 if best_ven_v else 0.0
        hid_score = best_hid_v / 255.0 if best_hid_v else 0.0
        adc_score = 0.0
        adc_p = None
        if adc_peak:
            adc_p = max(adc_peak, key=lambda q: adc_peak[q])
            adc_score = adc_peak[adc_p]
        if adc_score > 0.15:
            self.mapping.keys[key] = "adc"
            self.mapping.adc_pos[key] = adc_p
            _bk = "T1" if adc_p < 61 else "T2"
            src = f"ADC sensor {adc_p} (bank {_bk}, idx {adc_p % 61})"
            val = f"travel {adc_score:.2f}"
        elif ven_score > 0.15 and ven_score >= hid_score * 0.8:
            self.mapping.keys[key] = "vendor"
            self.mapping.vendor_pos[key] = best_ven_p
            src = f"vendor matrix pos {best_ven_p}"
            val = f"{best_ven_v/1000:.3f} mm"
        elif hid_score > 0.15:
            self.mapping.keys[key] = "hid"
            self.mapping.hid_byte[key] = best_hid
            src = f"HID analog byte {best_hid}"
            val = f"{best_hid_v}/255"
        else:
            log(f"calibrate {key}: no source above threshold "
                f"(adc {adc_score:.2f}, hid {best_hid_v}/255, "
                f"ven {best_ven_v/1000:.3f}mm)")
            return False
        try:
            self.mapping.save(MAPPING_PATH)
        except Exception as e:
            log_exception(f"calibrate save {key}", e)
        log(f"calibrated {key} → {src} (peak {val})")
        self._ui_queue_append(("notify", f"{key} → {src}  (peak {val})", GOOD))
        return True

    def _auto_calibrate(self):
        """Sequential auto calibration: wait for each WASD press (digital
        detection via MI_01), then sample its analog source."""
        if self._calib_running:
            log("calibrate ignored: already running", "WARN")
            return
        if not self.dkeys:
            log("calibrate ignored: digital-keys interface unavailable", "WARN")
            self._ui_queue_append(("notify",
                                   "Calibration needs the digital-keys "
                                   "interface (MI_01) — engine not fully up?", WARN))
            return
        self._calib_running = True
        self.calib_btn.configure(state="disabled")
        log("calibration started (interactive)")
        def worker():
            try:
                seq = ["W", "A", "S", "D"]
                for key in seq:
                    if not self.running:
                        break
                    self._ui_queue_append(("notify", f"Hold {key} for 2s…", ACCENT2))
                    # wait up to 10s for the key.  OS (GetAsyncKeyState) is
                    # the primary detector (works on this board); MI_01 HID
                    # is a secondary confirmation if it ever reports.
                    t_wait = time.time()
                    seen = False
                    while time.time() - t_wait < 10 and self.running:
                        os_ks = self.oskeys.poll()
                        if self.dkeys and self.dkeys.opened:
                            ks = self.dkeys.poll()
                            if self.dkeys._conv is None and os_ks:
                                self.dkeys.observe(os_ks)
                        else:
                            ks = set()
                        if key in ks or key in os_ks:
                            seen = True
                            log(f"calibration: {key} detected "
                                f"(hid={'Y' if key in ks else 'n'} "
                                f"os={'Y' if key in os_ks else 'n'})")
                            break
                        time.sleep(0.005)
                    if not seen:
                        raw = self.dkeys._raw_reports[-6:] if self.dkeys else []
                        raws = [bytes(r).hex() for r in raw]
                        log(f"calibration: missed {key} (not pressed in 10s) "
                            f"conv={getattr(self.dkeys, 'convention', None)} "
                            f"raw_recent={raws}", "WARN")
                        self._ui_queue_append(("notify", f"missed {key} — skipped", WARN))
                        continue
                    # baseline right after detection, sample while still held
                    pre_hid, pre_ven, _pre_adc = self._peek_baseline()
                    hid_max, ven_max, adc_peak = self._collect_spike(key, time.time(), 1.5)
                    hid_rise = [max(0, h - p) for h, p in zip(hid_max, pre_hid)]
                    ven_rise = {p2: max(0, v - pre_ven.get(p2, 0))
                                for p2, v in ven_max.items()}
                    ok = self._pick_source(key, hid_rise, ven_rise, adc_peak)
                    if not ok:
                        self._ui_queue_append(("notify", f"{key}: no analog source detected", WARN))
                    time.sleep(0.4)
                if self.running:
                    self._ui_queue_append(("notify", "Calibration complete — mapping saved", GOOD))
            except BaseException as e:
                log_exception("calibration worker", e)
            finally:
                self._calib_running = False
                self._ui_queue_append(("btn_state", "normal"))
        threading.Thread(target=worker, daemon=True).start()

    def _calibrate_start(self):
        if not self.running:
            log("calibrate requested but engine not running", "WARN")
            messagebox.showinfo("Slice Pad", "Start the engine first, then calibrate.")
            return
        log("calibrate button pressed")
        self._auto_calibrate()

    # ---------------- engine control ----------------
    def toggle_engine(self):
        if not self.running:
            self._start()
        else:
            self._stop()

    def _start(self):
        log("engine start requested")
        # find interfaces
        ifs = find_interfaces()
        log(f"interfaces found: {list(ifs)}")
        if "vendor" not in ifs and "kbd" not in ifs:
            log("keyboard not found", "ERROR")
            messagebox.showerror("Slice Pad",
                                 "Chilkey Slice75 HE (VID 0x1CA3 PID 0x0701) not found.\n"
                                 "Is the keyboard connected over USB?")
            return
        self.vendor = None
        self.hid = None
        self.dkeys = None
        if "vendor" in ifs:
            self.vendor = VendorStream(ifs["vendor"])
            if not self.vendor.open():
                log("vendor interface open FAILED", "WARN")
                self.vendor = None
            else:
                log(f"vendor interface opened ({ifs['vendor']!r})")
        if "kbd" in ifs:
            self.hid = HidAnalog(ifs["kbd"])
            if self.hid.open():
                log(f"hid-analog interface opened ({ifs['kbd']!r})")
            else:
                log("hid-analog interface open FAILED", "WARN")
        if "keys" in ifs:
            self.dkeys = DigitalKeys(ifs["keys"])
            if self.dkeys.open():
                log(f"digital-keys interface opened ({ifs['keys']!r})")
            else:
                log("digital-keys interface open FAILED", "WARN")
        if self.vendor is None and (self.hid is None or not self.hid.opened):
            log("no usable keyboard interfaces", "ERROR")
            messagebox.showerror("Slice Pad",
                                 "Could not open the keyboard interfaces.\n"
                                 "Close the Chilkey web driver if it is running "
                                 "(it holds the vendor HID interface).")
            return
        # if the vendor stream is up but we have no mapping yet, auto-calibrate
        import os as _os
        _skip_auto = _os.environ.get("SLICE_PAD_NO_AUTOCAL") == "1"
        if (self.vendor and not self.mapping.vendor_pos) and not _skip_auto:
            self._ui_queue_append(("notify",
                                   "No key mapping found — running quick calibration…", ACCENT2))
            log("no mapping found → starting auto-calibration")
            self._auto_calibrate()
        # bridge
        self.bridge = GamepadBridge(self.cfg)
        ok, msg = self.bridge.open()
        # status
        src = []
        if self.vendor:
            src.append(f"vendor:{self.vendor.fw_info or 'app'}")
        if self.hid and self.hid.opened:
            src.append("hid-analog")
        self.engine_lbl.configure(text=f"running  ·  {', '.join(src)}", text_color=GOOD)
        self.status_lbl.configure(text="● running", text_color=GOOD)
        log(f"engine started: {' , '.join(src)}"
            + ("" if ok else f"  [DRY-RUN: {msg}]"))
        if not ok:
            self.status_lbl.configure(text=f"● dry-run ({msg})", text_color=WARN)
        self.start_btn.configure(text="■  Stop", fg_color=BAD, hover_color=BAD_HOVER)
        self.running = True
        self._stop_evt.clear()
        self.engine = threading.Thread(target=self._engine_loop, daemon=True)
        self.engine.start()

    def _stop(self):
        log("engine stop requested")
        self.running = False
        self._stop_evt.set()
        try:
            if self.engine:
                self.engine.join(timeout=1.5)
                self.engine = None
        except Exception as e:
            log_exception("engine join", e)
        if self.bridge:
            try:
                self.bridge.close()
            except Exception as e:
                log_exception("bridge.close", e)
            self.bridge = None
        if self.vendor:
            try:
                self.vendor.close()
            except Exception as e:
                log_exception("vendor.close", e)
            self.vendor = None
        if self.hid:
            try:
                self.hid.close()
            except Exception as e:
                log_exception("hid.close", e)
            self.hid = None
        if self.dkeys:
            try:
                self.dkeys.close()
            except Exception as e:
                log_exception("dkeys.close", e)
            self.dkeys = None
        self.start_btn.configure(text="▶   Connect & Start", fg_color=ACCENT,
                                 hover_color=ACCENT_HOVER)
        self.engine_lbl.configure(text="disconnected", text_color=DIM)
        self.status_lbl.configure(text="● idle", text_color=DIM)
        log("engine stopped, interfaces released")

    def _engine_loop(self):
        log("engine loop started (~1 kHz)")
        polls = 0
        try:
            while not self._stop_evt.is_set() and self.running:
                polls += 1
                try:
                    # OS-level press watcher (ground truth for WASD)
                    os_ks = self.oskeys.poll()
                    # keep digital key state fresh (used by calibration)
                    if self.dkeys and self.dkeys.opened:
                        ks = self.dkeys.poll()
                        # ground-truth confirmation of the HID bit convention
                        if self.dkeys._conv is None and os_ks:
                            self.dkeys.observe(os_ks)
                    else:
                        ks = set()
                    # union: OS events are a reliable press signal too —
                    # they come from the same hardware, so live auto-cal can
                    # see the key even while the HID convention is still
                    # settling.
                    held = {k for k in ("W", "A", "S", "D")
                            if (k in ks) or (k in os_ks)}
                    self._live_autocal(held)
                    self._refresh_baselines(held)
                    # refresh the 6-byte HID analog block
                    if self.hid and self.hid.opened:
                        self.hid.poll()
                    # pull analog values
                    vals = {}
                    for k in ("W", "A", "S", "D"):
                        vals[k] = read_key_value(self.mapping, k, self.vendor, self.hid)
                    # update UI queue (thread-safe, coalesced by _poll_ui)
                    self._ui_queue_append(("keys", vals))
                    # update gamepad
                    if self.bridge:
                        x16, y16 = self.bridge.update(vals["W"], vals["A"], vals["S"], vals["D"])
                        self._ui_queue_append(("pad", (x16, y16,
                                                       vals["W"], vals["A"], vals["S"], vals["D"])))
                except BaseException as e:
                    # never let one bad tick kill the engine; log + back off
                    log_exception("engine tick", e)
                    time.sleep(0.05)
                    continue
                time.sleep(0.001)  # ~1 kHz poll; bridge throttles pad.update()
        finally:
            log(f"engine loop finished after {polls} ticks", "INFO")

    # ---------------- UI pump ----------------
    def _poll_ui(self):
        # drain the thread-safe queue; keep only latest state of each kind
        latest_keys = None
        latest_pad = None
        notifs: list = []
        btn_state = None
        with self._ui_qlock:
            items = self._ui_queue
            self._ui_queue = []
        for item in items:
            kind = item[0]
            if kind == "keys":
                latest_keys = item[1]
            elif kind == "pad":
                latest_pad = item[1]
            elif kind == "notify":
                notifs.append(item[1:])
            elif kind == "btn_state":
                btn_state = item[1]
        for text, color in notifs:
            self._ui_notify(text, color)
        if btn_state is not None:
            self.calib_btn.configure(state=btn_state)
        if latest_keys is not None:
            for k, v in latest_keys.items():
                self._key_states[k] = v
                self.bars[k].set(v)
        if latest_pad is not None and self.bridge:
            x16, y16, vw, va, vs, vd = latest_pad
            gx = max(-1.0, min(1.0, vd - va))
            gy = max(-1.0, min(1.0, vw - vs))
            if self.cfg.invert_x:
                gx = -gx
            if self.cfg.invert_y:
                gy = -gy
            self.stick.set(x16 / 32767.0, y16 / 32767.0, gx, gy)
            self.stats["frames"] += 1
            self.mag_lbl.configure(
                text=f"magnitude {self.bridge.magnitude:.2f}  ·  "
                     f"raw (pre-normalize) ghost shown hollow")
        self._update_telemetry()
        try:
            self.after(40, self._poll_ui)
        except Exception as e:
            # window already destroyed (e.g. _on_close raced) — stop quietly
            log_exception("ui poll reschedule (window gone?)", e)

    def _update_telemetry(self):
        parts = []
        if self.running:
            if self.vendor:
                parts.append(f"vendor frames {self.vendor.frame_count}")
                if self.vendor.stream_dead:
                    # firmware ADC task hung (control plane still answers).
                    # Surface a clear, actionable diagnostic.
                    parts.append("⚠ ADC STREAM DEAD — unplug & replug the keyboard")
            if self.dkeys:
                parts.append(f"keys conv={self.dkeys.convention} "
                             f"hits={self.dkeys._conv_hits}")
            parts.append(f"engine polls {self.stats['frames']}")
            parts.append(f"cfg {self.cfg.normalize_mode}/{self.cfg.stick}")
            if self.bridge and self.bridge.dry_run:
                parts.append("DRY-RUN (no virtual pad)")
            # reflect the stream-dead state in the header status dot
            if self.vendor and self.vendor.stream_dead:
                self.status_lbl.configure(text="● ADC stream dead — replug USB",
                                          text_color=WARN)
        else:
            parts.append("idle")
        self.tele_lbl.configure(text="\n".join(f"·  {p}" for p in parts))

    def _on_close(self):
        log("window close → shutting down")
        self._stop()
        try:
            self.destroy()
        except Exception as e:
            log_exception("window destroy", e)

    # ---------- frameless window chrome (overrideredirect) ----------
    def _apply_chrome(self):
        """Post-map hook: make sure the frameless window is mapped/focused.

        Acrylic translucency is a nice-to-have; the window is fully usable
        without it, so this never crashes even on hosts where the win32
        composition APIs are unavailable.
        """
        try:
            self.update_idletasks()
            self.lift()
        except Exception:
            pass

    def _force_focus(self):
        try:
            self.focus_force()
            self.lift()
        except Exception:
            pass

    def _toggle_max(self):
        try:
            if self._maximized:
                if self._normal_geom:
                    self.geometry(self._normal_geom)
                self._maximized = False
                self.max_btn.configure(text="□")
            else:
                self._normal_geom = self.geometry()
                self.state("zoomed")
                self._maximized = True
                self.max_btn.configure(text="❐")
        except Exception as e:
            log_exception("toggle maximize", e)

    def _minimize(self):
        try:
            # overrideredirect windows DO get a taskbar button on iconify,
            # and deiconify from the taskbar button restores them. (A
            # withdraw+timer would either flash back or strand the window
            # with no way to get it back.)
            self.iconify()
        except Exception as e:
            log_exception("minimize", e)

    def _drag_start(self, event):
        self._drag_dx = event.x
        self._drag_dy = event.y

    def _drag_move(self, event):
        if self._maximized:
            return
        x = self.winfo_x() + (event.x - self._drag_dx)
        y = self.winfo_y() + (event.y - self._drag_dy)
        self.geometry(f"+{x}+{y}")


def main():
    install_hooks()
    touch_latest()
    app = App()
    try:
        app.mainloop()
    except BaseException as e:  # noqa: BLE001 - last-resort net
        log_exception("mainloop", e)
        raise


if __name__ == "__main__":
    main()
