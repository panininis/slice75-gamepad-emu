"""Headless WASD→XInput engine core for Slice Pad.

The engine logic (interface handling, live auto-calibration, interactive
calibration, the ~1 kHz poll loop) lives here, independent of any UI.
Both front-ends drive this exact code:
  * the tkinter desktop app (app.py)
  * the local web UI (app_web.py)

UI side-effects are abstracted to ``self._ui_queue_append(item)`` with:
    ("keys",      {W,A,S,D: 0..1})
    ("pad",       (x16, y16, vw, va, vs, vd))
    ("notify",    text, color)
    ("btn_state", "normal" | "disabled")
"""
from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from slice_capture import (  # noqa: E402
    DigitalKeys, HidAnalog, Mapping, OsKeys, VendorStream,
    find_interfaces, read_key_value,
)
from gamepad_bridge import GamepadBridge  # noqa: E402
from applog import log, log_exception  # noqa: E402

MAPPING_PATH = os.path.join(os.path.expanduser("~"), ".slice-pad", "mapping.json")

# palette constants (match app.py theme) so notify colors look the same
GOOD = "#34d399"
WARN = "#fbbf24"
BAD = "#fb7185"
ACCENT2 = "#22d3ee"


class GamepadEngine:
    """The whole WASD→gamepad pipeline, independent of any UI.

    cfg:  GamepadConfig (UI mutates fields; engine reads them per tick —
          plain attribute access, GIL-safe).
    emit: callable(item) — receives the events above.  May be called from
          the engine thread, so implementations must be thread-safe.
    """

    def __init__(self, mapping: Mapping, cfg, emit):
        self.mapping = mapping
        self.cfg = cfg
        self._emit = emit
        self.oskeys = OsKeys()      # OS-level WASD press watcher (win32)
        self.vendor = None
        self.hid = None
        self.dkeys = None
        self.bridge = None
        self.running = False
        self._starting = False
        self._stop_evt = threading.Event()
        self.engine: threading.Thread | None = None
        self._calib_running = False
        self._lc: dict = {}         # live auto-cal tracking: key -> state
        # pre-press baseline per key: {key: {"hid","ven","adc","t"}} —
        # refreshed by the engine loop while the key is NOT held, so
        # calibration scores *rise from baseline*.
        self._key_baseline: dict = {}
        self.stats = {"frames": 0, "polls": 0}

    # ---------------------------------------------------------------- emit
    def _ui_queue_append(self, item):
        """Single funnel for all engine→UI events (thread-safe: delegates
        to self._emit, which the UI implements thread-safely)."""
        try:
            self._emit(item)
        except Exception as e:  # never let a broken UI kill the engine
            log_exception("emit", e)

    # ------------------------------------------------------------ helpers
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

    # -------------------------------------------------------- calibration
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
        adc_nz = {p2: v for p2, v in adc_max.items() if v > 0.05}
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
            self._ui_queue_append(("notify", "Start the engine first, then calibrate.", WARN))
            return
        log("calibrate button pressed")
        self._auto_calibrate()

    # ------------------------------------------------------- engine control
    def toggle(self):
        if not self.running:
            self.start()
        else:
            self.stop()

    def start(self):
        # idempotent: repeated clicks / double-taps are harmless
        if self.running:
            log("engine start ignored: already running", "WARN")
            return
        if self._starting:
            log("engine start ignored: start already in progress", "WARN")
            return
        self._starting = True
        log("engine start requested")
        # find interfaces
        ifs = find_interfaces()
        log(f"interfaces found: {list(ifs)}")
        if "vendor" not in ifs and "kbd" not in ifs:
            log("keyboard not found", "ERROR")
            self._starting = False
            self._ui_queue_append(("notify",
                                   "Chilkey Slice75 HE not found — is it "
                                   "connected over USB?", BAD))
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
            self._starting = False
            self._ui_queue_append(("notify",
                                   "Could not open the keyboard interfaces — "
                                   "close the Chilkey web driver if it is "
                                   "running (it holds the vendor HID).", BAD))
            return
        # if the vendor stream is up but we have no mapping yet, auto-calibrate
        _skip_auto = os.environ.get("SLICE_PAD_NO_AUTOCAL") == "1"
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
        log(f"engine started: {' , '.join(src)}"
            + ("" if ok else f"  [DRY-RUN: {msg}]"))
        if not ok:
            self._ui_queue_append(("notify", f"dry-run: {msg}", WARN))
        self.running = True
        self._starting = False
        self._stop_evt.clear()
        self.engine = threading.Thread(target=self._engine_loop, daemon=True)
        self.engine.start()

    def stop(self):
        log("engine stop requested")
        self.running = False
        self._starting = False
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
        log("engine stopped, interfaces released")

    # ------------------------------------------------------------ engine loop
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
                    # emit analog values (coalesced by the UI)
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
            self.stats["polls"] = polls
            log(f"engine loop finished after {polls} ticks", "INFO")
