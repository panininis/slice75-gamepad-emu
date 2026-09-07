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

from slice_capture import Mapping  # noqa: E402
from gamepad_bridge import GamepadConfig  # noqa: E402
from engine_core import GamepadEngine, MAPPING_PATH  # noqa: E402
from applog import (  # noqa: E402
    log, log_exception, install_hooks, touch_latest, LOG_FILE,
)

APP_DIR = os.path.join(os.path.expanduser("~"), ".slice-pad")
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

        self.cfg = self._load_config()
        self._mapping = Mapping.load(MAPPING_PATH)
        # The WASD->gamepad pipeline is the SHARED engine (engine_core) —
        # the same class the web UI drives, so both front-ends behave
        # identically and bugs are fixed in one place.  The GUI reads it
        # through read-through properties (self.vendor, self.bridge, ...).
        self._eng = GamepadEngine(self._mapping, self.cfg, self._ui_queue_append)
        self._ui_queue: list = []
        self._ui_qlock = threading.Lock()      # queue is touched by engine + UI threads
        self._ui_dirty = threading.Event()
        log("app starting", "INFO")
        log(f"python {sys.version.split()[0]}  |  log file: {LOG_FILE}")
        self._key_states = {"W": 0.0, "A": 0.0, "S": 0.0, "D": 0.0}
        self._curve_error = ""
        self._cal_key = None
        self._eng_ui_state = ("idle",)

        self._build_ui()
        self.after(100, self._poll_ui)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- shared engine accessors ----------------
    # Read-through properties so GUI code keeps reading self.vendor /
    # self.bridge / self.running / self.stats while the state lives in the
    # shared GamepadEngine.
    @property
    def running(self) -> bool:
        return self._eng.running

    @property
    def vendor(self):
        return self._eng.vendor

    @property
    def hid(self):
        return self._eng.hid

    @property
    def dkeys(self):
        return self._eng.dkeys

    @property
    def bridge(self):
        return self._eng.bridge

    @property
    def oskeys(self):
        return self._eng.oskeys

    @property
    def stats(self):
        return self._eng.stats

    @property
    def mapping(self):
        return self._eng.mapping

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
        # save every GamepadConfig field (dataclasses.fields) — a hard-coded
        # key list here silently dropped newer options (block_y_on_trigger,
        # ...) the next time the desktop app saved, reverting web-UI settings.
        from dataclasses import fields
        d = {f.name: getattr(self.cfg, f.name) for f in fields(GamepadConfig)}
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

    def _calibrate_start(self):
        if not self._eng.running:
            log("calibrate requested but engine not running", "WARN")
            messagebox.showinfo("Slice Pad", "Start the engine first, then calibrate.")
            return
        log("calibrate button pressed")
        self._eng._calibrate_start()

    # ---------------- engine control ----------------
    def toggle_engine(self):
        if self._eng.running or self._eng._starting:
            self._stop()
        else:
            self._start()

    def _start(self):
        """GUI entry point: start the shared engine on a worker thread.
        Interface-open takes ~1.4 s; button/labels update via _poll_ui from
        the engine's live state, so the GUI never freezes."""
        threading.Thread(target=self._eng.start, daemon=True).start()

    def _stop(self):
        """GUI entry point: stop off the UI thread (join + close ~2 s)."""
        threading.Thread(target=self._eng.stop, daemon=True).start()

    def _stop_sync(self):
        """Blocking stop for shutdown (window close)."""
        try:
            self._eng.stop()
        except BaseException as e:
            log_exception("engine stop (sync)", e)

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
        self._update_engine_ui()
        self._update_telemetry()
        try:
            self.after(40, self._poll_ui)
        except Exception as e:
            # window already destroyed (e.g. _on_close raced) — stop quietly
            log_exception("ui poll reschedule (window gone?)", e)

    def _update_engine_ui(self):
        """Reflect the shared engine's state on the button/labels.
        Called from the UI thread every ~40 ms (diff-gated)."""
        e = self._eng
        if e.running:
            srcs = []
            if e.vendor:
                srcs.append(f"vendor:{e.vendor.fw_info or 'app'}")
            if e.hid and e.hid.opened:
                srcs.append("hid")
            lbl = "running  ·  " + (", ".join(srcs) if srcs else "standalone")
            state = ("run", e.vendor.fw_info if e.vendor else "")
            color = WARN if (e.vendor and e.vendor.stream_dead) else GOOD
            if self._eng_ui_state != state:
                self._eng_ui_state = state
                self.engine_lbl.configure(text=lbl, text_color=color)
                self.status_lbl.configure(text="● running", text_color=color)
                self.start_btn.configure(text="■  Stop", fg_color=BAD,
                                         hover_color=BAD_HOVER, state="normal")
        elif e._starting:
            if self._eng_ui_state != ("starting",):
                self._eng_ui_state = ("starting",)
                self.status_lbl.configure(text="● connecting…", text_color=ACCENT2)
                self.start_btn.configure(state="disabled")
        else:
            if self._eng_ui_state != ("idle",):
                self._eng_ui_state = ("idle",)
                self.engine_lbl.configure(text="disconnected", text_color=FAINT)
                self.status_lbl.configure(text="● idle", text_color=DIM)
                self.start_btn.configure(text="▶   Connect & Start",
                                         fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                         state="normal")

    def _update_telemetry(self):
        e = self._eng
        parts = []
        if e.running:
            if e.vendor:
                parts.append(f"vendor frames {e.vendor.frame_count}")
                if e.vendor.stream_dead:
                    # firmware ADC task hung (control plane still answers).
                    # Surface a clear, actionable diagnostic.
                    parts.append("⚠ ADC STREAM DEAD — unplug & replug the keyboard")
                    self.status_lbl.configure(text="● ADC stream dead — replug USB",
                                              text_color=WARN)
            if e.dkeys:
                parts.append(f"keys conv={e.dkeys.convention} "
                             f"hits={e.dkeys._conv_hits}")
            parts.append(f"engine polls {e.stats['polls']}")
            parts.append(f"cfg {e.cfg.normalize_mode}/{e.cfg.stick}")
            if e.bridge and e.bridge.dry_run:
                parts.append("DRY-RUN (no virtual pad)")
        else:
            parts.append("idle")
        self.tele_lbl.configure(text="\n".join(f"·  {p}" for p in parts))

    def _on_close(self):
        log("window close → shutting down")
        self._stop_sync()
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
