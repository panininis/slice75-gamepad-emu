r"""Slice Pad — local web UI.

Serves a single-page glassmorphic dashboard (web/index.html) on
http://127.0.0.1:8321  and streams live state over Server-Sent Events.
The heavy lifting is the exact same engine as the desktop app
(engine_core.GamepadEngine), so behavior is identical.

API:
  GET  /            -> index.html
  GET  /state       -> latest JSON snapshot
  GET  /events      -> SSE stream (JSON snapshots, coalesced to screen rate)
  POST /api/start   -> engine.start()
  POST /api/stop    -> engine.stop()
  POST /api/calibrate
  POST /api/config  -> {"key": value, ...} applied + persisted
  POST /api/curve   -> {"expr": "v**2"}  (validated, live-applied)

Config persists to ~\.slice-pad\config.json (same file as the desktop app).
"""
from __future__ import annotations

import copy
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from slice_capture import Mapping  # noqa: E402
from gamepad_bridge import GamepadConfig, compile_curve  # noqa: E402
from applog import install_hooks, touch_latest, log, log_exception  # noqa: E402
from engine_core import GamepadEngine, MAPPING_PATH, GOOD, WARN, BAD, ACCENT2  # noqa: E402

WEB_DIR = os.path.join(HERE, "web")
APP_DIR = os.path.join(os.path.expanduser("~"), ".slice-pad")
CFG_PATH = os.path.join(APP_DIR, "config.json")
PORT = int(os.environ.get("SLICE_PAD_WEB_PORT", "8321"))

CONFIG_KEYS = ("normalize_mode", "stick", "invert_x", "invert_y", "deadzone",
               "gain", "curve", "curve_expr", "stick_deadzone", "max_update_hz",
               "trigger_w", "digital_fallback")


def load_config() -> GamepadConfig:
    cfg = GamepadConfig()
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH) as f:
                d = json.load(f)
            for k, v in d.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception as e:
            log(f"config load failed: {e}", "WARN")
    return cfg


def save_config(cfg: GamepadConfig) -> None:
    os.makedirs(APP_DIR, exist_ok=True)
    d = {k: getattr(cfg, k) for k in CONFIG_KEYS}
    with open(CFG_PATH, "w") as f:
        json.dump(d, f, indent=2)


class Hub:
    """Broadcasts coalesced state to all SSE clients."""

    def __init__(self, engine: GamepadEngine, cfg: GamepadConfig):
        self.engine = engine
        self.cfg = cfg
        self.lock = threading.Lock()
        self.clients: set[queue.Queue] = set()
        self.notif_id = 0
        self.state = {
            "running": False, "starting": False, "engine_lbl": "disconnected", "status": "idle",
            "status_color": "#8b95a7",
            "keys": {"W": 0.0, "A": 0.0, "S": 0.0, "D": 0.0},
            "pad": {"x": 0.0, "y": 0.0, "gx": 0.0, "gy": 0.0, "mag": 0.0},
            "fw": "", "vendor_frames": 0, "stream_dead": False,
            "dry_run": False, "polls": 0, "calib_running": False,
            "pad_updates": 0,
            "notifs": [],          # [[id, text, color], ...] newest last
        }

    # -- client management ------------------------------------------------
    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            self.clients.add(q)
        return q

    def drop_client(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    # -- engine emit (thread-safe, ~1 kHz) --------------------------------
    def emit(self, item):
        kind = item[0]
        if kind == "keys":
            with self.lock:
                self.state["keys"] = {k: round(v, 4) for k, v in item[1].items()}
                self._tick()
        elif kind == "pad":
            x16, y16, vw, va, vs, vd = item[1]
            cfg = self.cfg
            gx = max(-1.0, min(1.0, vd - va))
            gy = max(-1.0, min(1.0, vw - vs))
            if cfg.invert_x:
                gx = -gx
            if cfg.invert_y:
                gy = -gy
            mag = self.engine.bridge.magnitude if self.engine.bridge else 0.0
            with self.lock:
                self.state["pad"] = {"x": round(x16 / 32767.0, 4),
                                     "y": round(y16 / 32767.0, 4),
                                     "gx": round(gx, 4), "gy": round(gy, 4),
                                     "mag": round(mag, 4)}
                self.state["polls"] += 1
                self._tick()
        elif kind == "notify":
            _text, color = item[1], item[2]
            with self.lock:
                self.notif_id += 1
                self.state["notifs"].append([self.notif_id, _text, color])
                if len(self.state["notifs"]) > 8:
                    self.state["notifs"].pop(0)
                self._tick()
        elif kind == "btn_state":
            with self.lock:
                self.state["calib_running"] = item[1] == "disabled"

    def _tick(self):
        """Drop-oldest coalesced wake for every client (callers hold lock)."""
        for q in self.clients:
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def kick(self):
        """Force a wake of all clients (config changes) — call from any
        thread; the SSE loop coalesces it at screen rate."""
        with self.lock:
            self._tick()

    # -- derived state (called before every snapshot) ---------------------
    def _refresh_derived(self):
        e = self.engine
        cfg = self.cfg
        self.state["running"] = e.running
        self.state["starting"] = bool(e._starting)
        if e.running:
            src = []
            if e.vendor:
                src.append(f"vendor:{e.vendor.fw_info or 'app'}")
            if e.hid and e.hid.opened:
                src.append("hid")
            self.state["engine_lbl"] = "running · " + ", ".join(src) if src else "running"
            self.state["fw"] = e.vendor.fw_info if e.vendor else ""
            self.state["vendor_frames"] = e.vendor.frame_count if e.vendor else 0
            dead = bool(e.vendor and e.vendor.stream_dead)
            self.state["stream_dead"] = dead
            if dead:
                self.state["status"], self.state["status_color"] = "ADC stream dead — click engine button to recover", WARN
            elif self.state.get("dry_run"):
                self.state["status"], self.state["status_color"] = "dry-run", WARN
            else:
                self.state["status"], self.state["status_color"] = "running", GOOD
        elif e._starting:
            self.state["engine_lbl"] = "connecting…"
            self.state["status"], self.state["status_color"] = "starting", ACCENT2
        else:
            self.state["engine_lbl"] = "disconnected"
            self.state["status"], self.state["status_color"] = "idle", "#8b95a7"
            self.state["stream_dead"] = False
        self.state["dry_run"] = bool(e.bridge and e.bridge.dry_run)
        if e.bridge is not None:
            self.state["pad_updates"] = e.bridge.ticks
        self.state["calib_running"] = e._calib_running
        # echo current config so the UI always converges
        self.state["config"] = {k: getattr(cfg, k) for k in CONFIG_KEYS}

    def snapshot(self) -> dict:
        # Consistency snapshot: deep-copy under the lock, but do NOT hold
        # the lock for json.dumps — the SSE loop calls this at 30 Hz and
        # the engine's 1 kHz emit() blocks on this same lock, so serializing
        # while holding it made the hot path wait for the network.
        with self.lock:
            self._refresh_derived()
            return copy.deepcopy(self.state)


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    server_version = "SlicePad/1.0"
    hub: Hub = None  # set by main()

    def log_message(self, *a):  # keep the console quiet
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):  # noqa: N802
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif p == "/state":
            self._json(self.hub.snapshot())
        elif p == "/events":
            self._handle_sse()
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):  # noqa: N802
        p = self.path.split("?", 1)[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}
        if p == "/api/start":
            threading.Thread(target=self.hub.engine.start, daemon=True).start()
            self._json({"ok": True, "note": "start requested"})
        elif p == "/api/stop":
            threading.Thread(target=self.hub.engine.stop, daemon=True).start()
            self._json({"ok": True, "note": "stop requested"})
        elif p == "/api/restart":
            # in-place recovery for a hung ADC stream (reopening the vendor
            # endpoint revives the firmware's RM6X21 task — no USB replug)
            threading.Thread(target=self.hub.engine.restart, daemon=True).start()
            self._json({"ok": True, "note": "restart requested"})
        elif p == "/api/calibrate":
            threading.Thread(target=self.hub.engine._calibrate_start, daemon=True).start()
            self._json({"ok": True})
        elif p == "/api/config":
            self._apply_config(data)
        elif p == "/api/curve":
            expr = str(data.get("expr", "")).strip()
            ok = compile_curve(expr) is not None if expr else False
            if ok:
                self.hub.cfg.curve_expr = expr
                save_config(self.hub.cfg)
                if self.hub.engine.bridge:
                    self.hub.engine.bridge.set_curve(expr)
                self.hub.kick()
                self._json({"ok": True, "expr": expr})
            else:
                self._json({"ok": False, "error": "invalid expression"}, 400)
        else:
            self._send(404, "not found", "text/plain")

    def _apply_config(self, data: dict):
        cfg = self.hub.cfg
        changed = False
        for k in CONFIG_KEYS:
            if k in data:
                v = data[k]
                if k in ("invert_x", "invert_y", "trigger_w", "digital_fallback"):
                    v = bool(v)
                elif k == "max_update_hz":
                    v = int(round(float(v)))
                elif k in ("deadzone", "gain", "stick_deadzone"):
                    v = float(v)
                elif k in ("normalize_mode", "stick", "curve"):
                    v = str(v)
                if getattr(cfg, k, None) != v:
                    setattr(cfg, k, v)
                    changed = True
        if changed:
            save_config(cfg)
            # recompile curve if the expression or preset changed
            if self.hub.engine.bridge and ("curve" in data or "curve_expr" in data):
                self.hub.engine.bridge.set_curve(cfg.curve_expr)
            self.hub.kick()  # push fresh state so idle UIs reflect the change
        self._json({"ok": True})

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.hub.add_client()
        last_sent = 0.0
        try:
            # immediate first frame
            self.wfile.write(b"data: " + json.dumps(self.hub.snapshot()).encode() + b"\n\n")
            self.wfile.flush()
            last_sent = time.time()
            while True:
                try:
                    q.get(timeout=0.25)
                except queue.Empty:
                    pass
                # 30 Hz while the engine runs (smooth stick), 3 Hz heartbeat
                # while idle (still converges: config echo, engine state)
                MIN_INTERVAL = 0.033 if self.hub.engine.running else 0.33
                now = time.time()
                if now - last_sent < MIN_INTERVAL:
                    continue
                try:
                    self.wfile.write(b"data: " + json.dumps(self.hub.snapshot()).encode() + b"\n\n")
                    self.wfile.flush()
                    last_sent = now
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            self.hub.drop_client(q)


def main():
    install_hooks()
    touch_latest()
    cfg = load_config()
    mapping = Mapping.load(MAPPING_PATH)
    engine = GamepadEngine(mapping, cfg, None)  # emit wired below
    hub = Hub(engine, cfg)
    engine._emit = hub.emit

    Handler.hub = hub
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log(f"cannot bind 127.0.0.1:{PORT} — {e}", "ERROR")
        print(f"[Slice Pad] cannot bind 127.0.0.1:{PORT} (in use?). "
              f"Set SLICE_PAD_WEB_PORT to another port.", file=sys.stderr)
        raise SystemExit(1)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}"
    log(f"web ui listening on {url}")
    print(f"[Slice Pad] web UI: {url}   (Ctrl-C to stop)")
    # auto-start the engine when the app runs (keyboard must be plugged in)
    def _autostart():
        time.sleep(1.0)
        try:
            engine.start()
        except BaseException as e:
            log_exception("auto-start", e)
    threading.Thread(target=_autostart, daemon=True).start()
    if os.environ.get("SLICE_PAD_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[Slice Pad] stopping…")
    finally:
        engine.stop()
        srv.server_close()
        log("web ui stopped")


if __name__ == "__main__":
    main()
