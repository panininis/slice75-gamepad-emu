"""Slice75 HE analog capture for slice-pad.

Sources (both live, fused):
  1. VENDOR STREAM  (MI_02, usage page 0xFF00): real-time travel of all 126
     Hall-effect keys via the vendor protocol (65-byte HID reports on the
     wire: 1-byte report ID 0x00 + 64-byte payload).
        CMD 18 (KB2_CMD_RM6X21):
          (3, half) arm the stream, (2, half) read matrix, (6, half) read
          press-state matrix.  half=1 rows 0-2, half=2 rows 3-5 (6x21).
        Values: sub=2 -> 16-bit LE millimetres * 1000 (0.001 mm), sub=3 ->
        8-bit normalized press (0..255).
      Command frame layout (payload[0:64]):
        [0]=0x5C head  [1]=len (bytes from [4] through the first 0xFF)
        [2]=class 0x00 [3]=checksum = (53+b0+b1+b2+payload[len-1]) & 0xFF
        [4]=cmd        [5..]=params, terminated by 0xFF 0xFF.
      Device->host frame: [0]=0x5C [1]=len (0x92==192) [2]=class (cmd+128)
        [3]=sub  [4..]=data.
  2. HID MATRIX     (MI_00 standard keyboard report): 6 x 8-bit (0-255)
     analog channels for the six analog keys (the WASD group on this board).
     Bytes: [mod][reserved][a0..a5].  0 = released, 255 = bottomed.

Calibration:
  * The vendor stream gives us the absolute travel matrix; to map matrix
    positions to logical keys we run an interactive calibration: press W,
    A, S, D (and optionally the two extra analog keys) and record which
    matrix position / HID byte spikes.  The mapping is persisted to JSON.
  * If the vendor stream is unavailable (e.g. keyboard in boot mode), we
    fall back to the 6-byte HID analog block with a positional guess
    (W/A/S/D = bytes 0..3, extras = 4..5) — the user can re-map in the GUI.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

try:
    import hid as hidapi
except ImportError:  # pragma: no cover
    hidapi = None

# ---- Win32 1 ms timer resolution (refcounted) -------------------------------
# time.sleep(x < 0.015) rounds to the ~15.6 ms OS timer period unless the
# multimedia timer is raised to 1 ms.  Both the vendor ADC loop (sub-ms
# read windows) and the engine poll loop (1 ms / 8 ms adaptive ticks) need
# it; the refcount keeps it on while EITHER is running and restores the
# system timer when both stop.  No effect on board traffic.
_winmm = None
if sys.platform == "win32":
    try:
        _winmm = ctypes.windll.winmm
    except Exception:  # pragma: no cover
        _winmm = None
_timer_lock = threading.Lock()
_timer_refs = 0


def timer_res_begin() -> bool:
    """Request 1 ms timer resolution (refcounted).  Returns True on the
    first reference (the one that actually called timeBeginPeriod)."""
    global _timer_refs
    if _winmm is None:
        return False
    with _timer_lock:
        _timer_refs += 1
        first = _timer_refs == 1
    if first:
        try:
            _winmm.timeBeginPeriod(1)
        except Exception:  # pragma: no cover
            pass
    return first


def timer_res_end() -> bool:
    """Release one reference.  Returns True on the last one (the one that
    actually called timeEndPeriod)."""
    global _timer_refs
    if _winmm is None:
        return False
    with _timer_lock:
        _timer_refs = max(0, _timer_refs - 1)
        last = _timer_refs == 0
    if last:
        try:
            _winmm.timeEndPeriod(1)
        except Exception:  # pragma: no cover
            pass
    return last

VID = 0x1CA3
PID = 0x0701

HEAD = 0x5C
# NOTE: command IDs are the DECIMAL values from the web driver's enum
# (KB2_CMD_SYNC:1, KB2_CMD_RM6X21:18, KB2_CMD_DB:41 ...).
CMD_SYNC = 0x01
CMD_RM6X21 = 0x12          # decimal 18 — real-time travel stream
CMD_DB = 0x29              # decimal 41 — global settings
CMD_START_ADJUSTING = 0x0C  # decimal 12 — begin real-time adjusting session
CMD_SAVE_ADJUSTING = 0x0D   # decimal 13 — end the adjusting session
SUB_ARM = 3
SUB_READ_MM = 2            # 16-bit LE mm*1000 (zero unless a board-side
                           #   calibration was saved via the web driver)
SUB_READ_PRESS = 3         # 8-bit press 0..255
SUB_READ_ADC = 6           # raw Hall-effect ADC — LIVE without any board
                           #   calibration; the primary analog source.
SUB_READ_ADC_ALT = 5       # alternate sensor stream (board also answers 5)
SUB_READ_RAW = 11          # raw sensor stream #11 (different cells; W may
                           #   live here)
# Firmware 1.1.7.3 response geometry (measured on the live board and
# confirmed against the vendor web driver's RM6X21 response handler):
#   one sub-6 answer is a 128-byte frame delivered as TWO 64-byte reports:
#     report 1 (header):      [0]=0x5C [1]=0x80(128) [2]=0x92 [3]=0xa3/0xa4
#                             [4]=0x00 [5]=sub [6..63] = cells 0..28
#     report 2 (continuation): [0..63] = cells 29..60
#   (an all-zero 64-byte padding report follows each frame — ignored).
#   61 cells = the full 6x21 sensor matrix per frame (the vendor driver's
#   3x21=63-cell parse counts 2 header bytes as "cells").
#   The board ROTATES the two sensor banks (T1/T2) continuously, ignoring
#   the `half` request param, so the bank is classified from the frame's
#   own dead-cell signature (cells 12 & 14 alive = T1, dead = T2).
ADC_CELLS_PER_HALF = 61    # pos = bank*ADC_CELLS_PER_HALF + idx (idx 0..60)
ADC_FULL_DEFAULT = 1250.0 # raw-count drop of a full keypress (measured
                          #   ~1190..1250 on W/A/S/D of this board)
RESP_OFFSET = 128          # device->host class = cmd + 128 (decimal)
RESP_RM6X21 = CMD_RM6X21 + RESP_OFFSET   # 146 = 0x92
RESP_SYNC = CMD_SYNC + RESP_OFFSET       # 0x81

N_ROWS, N_COLS = 6, 21
N_KEYS = N_ROWS * N_COLS

# WASD default channel assignments (HID analog byte index, 0-based)
DEFAULT_HID_MAP = {"W": 0, "A": 1, "S": 2, "D": 3}
DEFAULT_EXTRA_HID = (4, 5)


def _checksum(b0: int, b1: int, b2: int, last: int) -> int:
    return (53 + b0 + b1 + b2 + last) & 0xFF


def build_cmd(cmd: int, params: tuple[int, ...]) -> bytes:
    """64-byte vendor command payload (report ID 0x00 is added by hidapi).

    Byte layout (matches the web driver's CMDPack/RM6X21Pack exactly):
      [0]=0x5C head   [1]=len (4 for a 2-param command)
      [2]=CMD class   [3]=checksum = (53 + b0 + b1 + b2 + b[last+1]) & 0xFF
      [4..]=params, padded with 0xFF to fill the len field's span.
    """
    r = bytearray(64)
    a = 4
    for v in params:
        r[a] = v
        a += 1
    while a < 6:          # pad the fixed 2-param tail with 0xFF 0xFF
        r[a] = 0xFF
        a += 1
    r[0] = HEAD
    r[1] = 4
    r[2] = cmd
    r[3] = _checksum(r[0], r[1], r[2], r[7])
    return bytes(r)


def build_sync() -> bytes:
    r = bytearray(64)
    a = 4
    for v in (1, 2, 3, 4):
        r[a] = v
        a += 1
    r[0] = HEAD
    r[2] = 1
    r[1] = a - 4
    r[3] = _checksum(r[0], r[1], r[2], r[r[1] + 3])
    return bytes(r)


class VendorStream:
    """Reads the real-time travel matrix from the MI_02 vendor interface.

    Runs a poll thread that issues RM6X21(6, half) (raw Hall ADC) alternately
    for halves 1/2 at ~100-150 Hz per half (measured 200+ req/s capable) and
    a reader thread that parses the 0x92 single-report frames.

    Data (all keyed by pos = (half-1)*63 + idx, idx 0..28):
      * `adc`   raw ADC counts (drops ~1200 on a full press, rest ~2900-3000)
      * `mm`    legacy 16-bit mm values (zero unless a board-side calibration
                was saved via the web driver) — kept as a secondary source
      * `travel_snapshot()` -> {pos: 0..1} normalized via a one-sided
        per-sensor baseline + full-press range (self-calibrating)

    The adjusting session is deliberately NOT started: the ADC answers plain
    queries, and leaving that session open is what makes the keyboard feel
    sluggish.
    """

    def __init__(self, path: bytes, poll_period: float = 0.003):
        # The board answers ADC polls in ~2-3 ms (measured).  3 ms period ->
        # ~100-150 Hz per half, limited by round-trip latency.
        self.path = path
        self.poll_period = max(0.002, min(0.05, poll_period))
        self.latest: dict[int, tuple[int, int]] = {}
        self.mm: dict[int, int] = {}
        self.press: dict[int, int] = {}
        self.adc: dict[int, int] = {}
        self.adc_alt: dict[int, int] = {}     # sub 5
        self.adc_raw: dict[int, int] = {}     # sub 11
        self._adc_ring: dict[int, deque] = {}    # rolling raw history (maxlen-bounded)
        self._adc_base: dict[int, int] = {}         # rest baseline (ratchets up)
        self._ring_n: dict[int, int] = {}           # consecutive rest-near-max samples
        self._ring_gen: dict[int, int] = {}         # per-sensor sample generation
        self._ring_max_gen: dict[int, int] = {}     # gen the cached max() is valid for
        self._ring_max: dict[int, int] = {}         # cached max(ring) — O(300) only on new sample
        self._RING_LEN = 300               # ~1.2 s at 250 Hz/sensor
        self.fw_info: str = ""
        self.frame_count = 0
        self.running = False
        # ADC stream health: the firmware's RM6X21 handler can HANG at the
        # task level (e.g. after several processes fight over the vendor
        # endpoint).  The control plane (SYNC 0x81) keeps answering while
        # 0x92 ADC frames stop entirely.  Watchdog: no ADC frame for 3 s
        # -> stream_dead=True (GUI/log surface a clear diagnostic:
        # "unplug & replug the keyboard").
        self._last_adc_frame: float = 0.0
        self._stream_dead: bool = False
        self._stream_dead_at: float = 0.0
        self._buf = bytearray()
        self._half_flip = True
        self._stop = threading.Event()
        self._dev = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        # NOTE: no request-order FIFO.  The 0x92 frame does NOT encode the
        # half (identical headers for halves 1 and 2), so the half is known
        # only from the request just sent -> the ADC loop is synchronous
        # (one outstanding request at a time) and labels each response with
        # the half it asked for.  This is what keeps the WASD cells (which
        # live in half 2) correctly addressed.

    # ---- lifecycle -------------------------------------------------
    def open(self) -> bool:
        if hidapi is None:
            return False
        try:
            self._dev = hidapi.device()
            self._dev.open_path(self.path)
            self._dev.set_nonblocking(True)
        except OSError:
            return False
        # clear any stuck I/O on the endpoint
        self._clear_stuck_io()
        # handshake: SYNC, capture the info frame (App-mode check + fw)
        try:
            self._dev.write(b"\x00" + build_sync())
        except OSError:
            return False
        time.sleep(0.35)
        self._drain()
        # NOTE: no adjusting session.  The raw ADC (sub 6) answers plain
        # queries; that session is what leaves the keyboard sluggish if it
        # is left open by a crashed process.
        self.running = True
        self._stop.clear()
        self._last_adc_frame = time.time()   # watchdog starts here
        self._stream_dead = False
        self._threads = [
            threading.Thread(target=self._adc_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        # the ADC loop uses sub-ms read windows — 1 ms timer resolution
        # (refcounted; shared with the engine poll loop)
        timer_res_begin()
        return True

    def snapshot(self) -> dict[int, int]:
        """Thread-safe copy of the travel matrix {pos: mm}."""
        with self._lock:
            return dict(self.mm)

    def start_adjusting(self) -> None:
        """Begin the real-time adjusting session (web driver CMDOrder 12)."""
        if self._dev is None:
            return
        try:
            self._dev.write(b"\x00" + build_cmd(CMD_START_ADJUSTING, ()))
        except OSError:
            pass

    def save_adjusting(self) -> None:
        """End the adjusting session (web driver CMDOrder 13)."""
        if self._dev is None:
            return
        try:
            self._dev.write(b"\x00" + build_cmd(CMD_SAVE_ADJUSTING, ()))
        except OSError:
            pass

    def close(self) -> None:
        self.running = False
        self._stop.set()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=0.6)
        self._threads = []
        timer_res_end()
        if self._dev is not None:
            # NOTE: we do NOT send save_adjusting (CMDOrder 13) on close.
            # We never start an adjusting session (start_adjusting), so an
            # unmatched "end adjusting" leaves the board's ADC task hung —
            # observed: the 0x92 stream goes dead for minutes after a run
            # that ends with save_adjusting.  The vendor only pairs 12/13.
            try:
                self._dev.close()
            except OSError:
                pass
            self._dev = None

    def _clear_stuck_io(self) -> None:
        """Cancel any pending I/O left by a crashed previous session."""
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            CF = k32.CreateFileW
            CF.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
                           ctypes.wintypes.DWORD, ctypes.c_void_p,
                           ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                           ctypes.wintypes.HANDLE]
            CF.restype = ctypes.wintypes.HANDLE
            h = CF(self.path.decode(), 0xC0000000, 0x3, None, 3, 0, None)
            if h and h != 0xFFFFFFFFFFFFFFFF:
                k32.CancelIoEx(h, None)
                k32.CloseHandle(h)
        except Exception:
            pass
        time.sleep(0.25)

    # ---- internals --------------------------------------------------
    # Bank signatures (idle, measured on this board, FW 1.1.7.3) over the
    # full 61-cell frame (29 header + 32 continuation cells).  The firmware
    # IGNORES the `half` request parameter: one sub-6 answer carries the
    # NEXT bank in a continuous T1,T2,T1,T2... rotation (~96-300 frames/s
    # depending on poll rate).  The bank is identified from the frame
    # itself:
    #   * dead-cell pattern (cells with no sensor are always 0):
    #       T1 (W bank): cells 12 & 14 alive (~2931); zeros at 15..20,
    #                    36..41, 57..60
    #       T2 (WASD bank): cells 12, 14 dead; zeros at 12, 14..20, 22,
    #                       33, 36..41, 45..47, 49..52, 58..60
    #   * fallback: idle reference values (c0,c1): T1 ~ (2915, 2885),
    #     T2 ~ (2981, 2929).  A keypress only drops its own cell ~1200,
    #     so the dead-cell pattern is the robust discriminator.
    _REF_T1 = (2915, 2885)
    _REF_T2 = (2981, 2929)

    @classmethod
    def _classify_bank(cls, cells: list[int]) -> str:
        """Return 'T1', 'T2', or '' for a full (61-cell) or header-only
        (29-cell) RM6X21 frame.

        Primary: dead-cell pattern (invariant wiring — cells 12 and 14
        are alive in T1, dead in T2).  Fallback: idle reference values.
        """
        if len(cells) < 29:
            return ""
        c12, c14 = cells[12], cells[14]
        if c12 > 500 and c14 > 500:
            return "T1"
        if c12 < 500 or c14 < 500:
            return "T2"
        # ambiguous (partial/garbage frame): fall back to reference values
        dT1 = abs(cells[0] - cls._REF_T1[0]) + abs(cells[1] - cls._REF_T1[1])
        dT2 = abs(cells[0] - cls._REF_T2[0]) + abs(cells[1] - cls._REF_T2[1])
        if dT1 and dT2:
            return "T1" if dT1 < dT2 else "T2"
        return ""

    def _adc_loop(self) -> None:
        """Continuous RM6X21 poll, ONE request outstanding at a time.

        The `half` request parameter is ignored by the firmware: it
        ROTATES the two sensor banks (T1/T2) continuously, so each answer
        is classified from its own dead-cell signature, never from the
        request.  One sub-6 answer spans TWO 64-byte reports (29-cell
        header + 32-cell continuation = 61 cells); the header must be
        paired with the following non-header report BEFORE parsing, or
        half the sensors (including W, continuation idx 44) are lost.

        Strictly synchronous (one outstanding request, 8 ms read window):
        the board also pushes unsolicited frames, so a header carries
        across windows (pending until its continuation arrives) and a
        newer header pre-empts a stale pending one.
        """
        half = 1
        hdr = None            # pending sub-6 header awaiting continuation
        while self.running and not self._stop.is_set():
            try:
                self._dev.write(b"\x00" + build_cmd(CMD_RM6X21, (SUB_READ_ADC, half)))
            except OSError:
                time.sleep(0.002)
                continue
            deadline = time.time() + 0.008
            got = False
            while time.time() < deadline and self.running and not self._stop.is_set():
                try:
                    rep = self._dev.read(65)
                except OSError:
                    break
                if rep:
                    b = self._norm(rep)
                    if b and b[0] == HEAD and b[2] == 0x92:
                        if b[5] == SUB_READ_ADC:   # sub-6 travel header
                            if hdr is None:
                                hdr = b            # await continuation
                                got = True
                            else:
                                # pre-empted: drain the stale header with
                                # whatever continuation this is (best effort
                                # — a header here means the real continuation
                                # was missed; classify from the header alone
                                # is wrong, so parse header-only cells)
                                self._process_92(hdr, cont=None)
                                hdr = b
                                got = True
                        elif b[5] in (SUB_READ_MM, SUB_READ_PRESS):
                            # sub-2/3 answers arrive as a single header
                            # (their data starts at [6]); handle inline
                            self._process_92(b, cont=None)
                            got = True
                    elif b and b[0] == HEAD and b[2] == 0x81:
                        self._process_sync(b)
                    elif hdr is not None:
                        # non-header report after a pending header = the
                        # continuation (32 cells, raw, no 0x5C head)
                        c = self._strip_id(rep)
                        if c is not None and any(c):
                            self._process_92(hdr, cont=c)
                            hdr = None
                            got = True
                time.sleep(0.0003)
            # advance the half (harmless: the board ignores it, but keep
            # request/response lockstep for the 8 ms windowing)
            half = 2 if half == 1 else 1
            if not got:
                time.sleep(0.0005)
            # stream watchdog
            if not self._stream_dead:
                if time.time() - self._last_adc_frame > 3.0:
                    self._stream_dead = True
                    self._stream_dead_at = time.time()
            elif time.time() - self._stream_dead_at > 5.0:
                # retry probe once per 5 s; clear if a frame arrives
                # (self-heal) — otherwise the device needs replugging.
                self._stream_dead_at = time.time()

    @property
    def stream_dead(self) -> bool:
        """True when no RM6X21 ADC frame has arrived for >3 s (firmware
        ADC handler hung; control plane still answers).  Recovery:
        unplug & replug the keyboard (settings are retained)."""
        return self._stream_dead

    def _mark_adc_frame(self) -> None:
        self._last_adc_frame = time.time()
        if self._stream_dead and time.time() - self._stream_dead_at > 2.0:
            self._stream_dead = False

    def _drain(self, timeout: float = 0.6) -> None:
        t0 = time.time()
        hdr = None
        while time.time() - t0 < timeout:
            try:
                rep = self._dev.read(65)
            except OSError:
                break
            if rep:
                b = self._norm(rep)
                if b and b[0] == HEAD:
                    if b[2] == 0x92:
                        if b[5] == SUB_READ_ADC:
                            if hdr is None:
                                hdr = b
                            else:
                                self._process_92(hdr, cont=None)
                                hdr = b
                        else:
                            self._process_92(b, cont=None)
                    elif b[2] == 0x81:
                        self._process_sync(b)
                    else:
                        self._feed(b)
                elif hdr is not None:
                    c = self._strip_id(rep)
                    if c is not None and any(c):
                        self._process_92(hdr, cont=c)
                        hdr = None
            else:
                time.sleep(0.005)
        if hdr is not None:
            self._process_92(hdr, cont=None)

    def _feed(self, data: bytes) -> None:
        """Each 64-byte report embeds one frame at offset 0 (head 0x5C) with
        zero padding after the frame.  Frames with L==0x92 (146) span three
        reports and are reassembled from the buffer."""
        self._buf.extend(data)
        while len(self._buf) >= 2:
            if self._buf[0] != HEAD:
                del self._buf[:1]
                continue
            L = self._buf[1]
            if L == 0x92:
                L = 192
            if L < 2:
                # padding / spurious: drop these two bytes and resync
                del self._buf[:2]
                continue
            need = L + 2
            if len(self._buf) < need:
                break
            frame = bytes(self._buf[:need])
            del self._buf[:need]
            self._process_frame(frame)
        if len(self._buf) > 512:
            del self._buf[: len(self._buf) - 256]

    @staticmethod
    def _norm(rep) -> bytes:
        """Normalize a raw HID read: strip a leading 0x00 report ID if one
        is present (65-byte read of a 64-byte report).  Returns the 64-byte
        payload starting with 0x5C, or b'' for empty/garbage reads."""
        b = bytes(rep) if isinstance(rep, (list, tuple)) else bytes(rep)
        if not b:
            return b""
        if len(b) == 65 and b[0] == 0:
            b = b[1:]
        if len(b) < 3 or b[0] != HEAD:
            return b""
        return b

    @staticmethod
    def _strip_id(rep) -> bytes:
        """Strip the leading 0x00 report ID (65-byte read of a 64-byte
        report).  Returns the raw 64-byte payload — used for CONTINUATION
        reports, which do NOT carry the 0x5C head.  b'' for empty reads."""
        b = bytes(rep) if isinstance(rep, (list, tuple)) else bytes(rep)
        if not b:
            return b""
        if len(b) == 65 and b[0] == 0:
            b = b[1:]
        return b[:64]

    def _process_sync(self, data: bytes) -> None:
        """SYNC info report (class 0x81): extract the App/Boot fw string."""
        import re
        txt = data.decode("latin-1", "replace")
        m = re.search(r"(App|Boot)\s*(V[\d.]+)?", txt)
        if m:
            mode = m.group(1)
            self.fw_info = mode + (f" v{m.group(2)[1:]}" if m.group(2) else "")

    def _process_92(self, data: bytes, cont: bytes | None = None) -> None:
        """Parse an RM6X21 travel frame.

        `data` is the 64-byte HEADER report:
          [0]=0x5C head [1]=0x80 [2]=0x92 class [3]=0xa3/0xa4 [4]=0x00
          [5]=sub  [6..63]=cells 0..28 (uint16 LE)
        `cont` is the optional 64-byte CONTINUATION report (raw, no head):
          [0..63]=cells 29..60  (sub-6 only; the board also pushes an
          all-zero padding report after each frame — never parsed).

        Full frame = 61 cells (the 6x21 matrix; vendor driver's 63-cell
        loop counts 2 header bytes as cells).  Bank: classified from the
        frame's dead-cell signature (cells 12 & 14 — always in the header)
        — the board rotates T1/T2 and ignores the request's half param.
        pos = base + idx with base 0 (T1) or 61 (T2).
        """
        if len(data) < 7:
            return
        sub = data[5]
        hdr_cells = min((len(data) - 6) // 2, 29)
        cells = [int.from_bytes(data[6 + 2 * i: 8 + 2 * i], "little")
                 for i in range(hdr_cells)]
        if cont is not None and len(cont) >= 64 and sub == SUB_READ_ADC:
            cells += [int.from_bytes(cont[2 * i: 2 + 2 * i], "little")
                      for i in range(min(32, len(cont) // 2))]
        cells = cells[:ADC_CELLS_PER_HALF]
        bank = self._classify_bank(cells)
        if not bank:
            return
        base = 0 if bank == "T1" else ADC_CELLS_PER_HALF
        with self._lock:
            if sub == SUB_READ_ADC:
                for i in range(len(cells)):
                    pos = base + i
                    v = cells[i]
                    self.adc[pos] = v
                    # O(1) bounded ring: deque(maxlen) evicts the oldest
                    # sample on append.  The old list.append + pop(0) was
                    # O(n) (memmove of 300 pointers) per cell per frame —
                    # ~50k memmoves/sec at 660 frames/s x 32 cells.
                    self._adc_ring.setdefault(
                        pos, deque(maxlen=self._RING_LEN)).append(v)
                    self._ring_gen[pos] = self._ring_gen.get(pos, 0) + 1
                self.frame_count += 1
                self._mark_adc_frame()
            elif sub == SUB_READ_MM:
                for i in range(len(cells)):
                    pos = base + i
                    v = cells[i]
                    if v:
                        self.mm[pos] = v

    def _ring_max_cached(self, pos: int) -> int:
        """max() over the rolling ring, recomputed only when a new sample
        for `pos` lands (gen counter bumped in _process_92).  The engine
        calls travel() ~4x per tick at ~1 kHz, but the ring only changes
        at the ~124 Hz ADC frame rate — without this cache every call
        re-scanned all 300 samples (~1.2M comparisons/s wasted)."""
        ring = self._adc_ring.get(pos)
        if not ring:
            return 0
        gen = self._ring_gen.get(pos, 0)
        if self._ring_max_gen.get(pos) != gen:
            m = max(ring)
            self._ring_max[pos] = m
            self._ring_max_gen[pos] = gen
        return self._ring_max[pos]

    def travel(self, pos: int) -> float:
        """Normalized 0..1 analog travel for a sensor position.

        The Hall sensor rests high (~2900-3000) and drops ~1200-1250 counts
        on a full press.  Baseline handling (the "rest tracker"):

        * seeded with the MEDIAN of the first ~60 samples (~0.25 s) so a
          single noisy spike can't set a bad baseline (the classic bug);
        * then it only RATCHETS UP (rest is always the high end): it follows
          a new high after the sensor sits within 15 counts of the recent
          window max for 10 consecutive samples (~40 ms) — i.e. after a
          full release or genuine upward rest drift.  It never chases a
          press downward, so a held key keeps full travel for as long as it
          is held, and the stick returns to 0 ~40 ms after release.
        """
        with self._lock:
            raw = self.adc.get(pos)
            if raw is None:
                return 0.0
            ring = self._adc_ring.get(pos)
            base = self._adc_base.get(pos)
            if base is None:
                if len(ring) < 60:
                    return 0.0            # still seeding
                self._adc_base[pos] = sorted(ring)[len(ring) // 2]
                self._ring_n[pos] = 0
                return 0.0
            if raw >= self._ring_max_cached(pos) - 15:
                self._ring_n[pos] = self._ring_n.get(pos, 0) + 1
            else:
                self._ring_n[pos] = 0
            if self._ring_n[pos] >= 10 and raw > base:
                self._adc_base[pos] = raw
            delta = base - raw
        if delta <= 0:
            return 0.0
        return max(0.0, min(1.0, delta / ADC_FULL_DEFAULT))

    def travel_snapshot(self) -> dict[int, float]:
        """{pos: travel} for every sensor seen (thread-safe)."""
        with self._lock:
            pos_set = set(self.adc)
        return {p: self.travel(p) for p in pos_set}

    def _process_frame(self, data: bytes) -> None:
        # Legacy 0x5C variable-length stream.  RM6X21 travel (class 0x92)
        # and SYNC info (class 0x81) frames are routed to dedicated parsers
        # by the reader; only other vendor command responses reach here.
        cls = data[0]
        if cls == RESP_SYNC and len(data) > 20:
            # info frame: [4]=0x01 [5]=? [6]=fw byte ... ASCII app/fw strings
            # Layout (observed): 5c 3c 81 4d | 00 01 07 03 1f 00 04 00 10
            #   <serial ASCII> 10 <"App " + fw> ...
            try:
                txt = data[4:len(data)].decode("latin-1", "replace")
                if "App" in txt:
                    self.fw_info = "app"
                    # extract "Vx.y.z.w"
                    import re
                    m = re.search(r"V([\d.]+)", txt)
                    if m:
                        self.fw_info = f"app v{m.group(1)}"
                    else:
                        self.fw_info = "app"
                elif "Boot" in txt:
                    self.fw_info = "boot"
            except Exception:
                pass


class HidAnalog:
    """Reads the 6x 8-bit analog block from the MI_00 keyboard report.

    Report layout (8 data bytes, report ID 0):
      [0]=modifier  [1]=reserved  [2..7]=analog channels a0..a5 (0-255).
    `latest` is a list of 6 ints; `changed` events are coalesced by the
    app-level poller.
    """

    def __init__(self, path: bytes):
        self.path = path
        self.latest: list[int] = [0] * 6
        self.opened = False
        self._dev = None
        self._lock = threading.Lock()   # shared-handle guard (engine + calib threads)

    def open(self) -> bool:
        if hidapi is None:
            return False
        try:
            self._dev = hidapi.device()
            self._dev.open_path(self.path)
            self._dev.set_nonblocking(True)
            self.opened = True
            return True
        except OSError:
            return False

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except OSError:
                    pass
                self._dev = None
            self.opened = False

    def snapshot(self) -> list[int]:
        """Thread-safe copy of the latest 6 analog bytes."""
        with self._lock:
            return list(self.latest)

    def poll(self) -> None:
        """Drain pending reports and update `latest` (non-blocking, locked)."""
        with self._lock:
            if self._dev is None:
                return
            while True:
                try:
                    rep = self._dev.read(16)
                except OSError:
                    return
                if not rep:
                    return
                d = rep[1:] if (len(rep) == 9 and rep[0] == 0) else rep
                if len(d) >= 8:
                    self.latest = list(d[2:8])


# Standard HID USAGE values (Keyboard/Keypad page 0x07) for the keys the
# app cares about.  NOTE: HID keyboard reports carry usages, NOT scancodes
# (scancode 0x11 is W, but its HID usage is 0x1A).
_USAGE2K = {
    0x04: "A", 0x05: "B", 0x06: "C", 0x07: "D", 0x08: "E", 0x09: "F",
    0x0A: "G", 0x0B: "H", 0x0C: "I", 0x0D: "J", 0x0E: "K", 0x0F: "L",
    0x10: "M", 0x11: "N", 0x12: "O", 0x13: "P", 0x14: "Q", 0x15: "R",
    0x16: "S", 0x17: "T", 0x18: "U", 0x19: "V", 0x1A: "W", 0x1B: "X",
    0x1C: "Y", 0x1D: "Z",
    0x1E: "1", 0x1F: "2", 0x20: "3", 0x21: "4", 0x22: "5", 0x23: "6",
    0x24: "7", 0x25: "8", 0x26: "9", 0x27: "0",
    0x28: "ENTER", 0x29: "ESC", 0x2A: "BACKSPACE", 0x2E: "SPACE",
    0x30: "F1", 0x31: "F2", 0x32: "F3", 0x33: "F4",
    0x4F: "LEFT", 0x50: "RIGHT", 0x51: "UP", 0x52: "DOWN",
    0xE1: "SHIFT_L", 0xE5: "SHIFT_R",
}
# The MI_01 descriptor declares Usage Min 0x04 .. Max 0x63 packed into 96
# bits.  Bit order can be "spec" (bit b = usage 0x04+b) or "flat" (bit b =
# usage b) depending on firmware.  `_conv` is auto-detected from the first
# real keypress; until then BOTH conventions are reported.
_CONV_SPEC_OFFSET = 0x04


class DigitalKeys:
    """Reads the digital (actuated) key state from MI_01 Col01 (report ID 1).

    Report layout (14 bytes): [rid=0x01][mod][12 x 8 keys], bit i of byte b
    -> key index b*8+i -> Set-1 scancode.  `latest` is the most recent set of
    pressed key labels (W/A/S/D/...).
    """

    def __init__(self, path: bytes):
        self.path = path
        self.latest: set[str] = set()
        self.opened = False
        self._dev = None
        self._lock = threading.Lock()   # shared-handle guard
        self._conv: int | None = None   # 0 = flat, 1 = spec(+0x04); None = unknown
        self._conv_hits = [0, 0]        # per-convention known-label hits
        self._raw_reports: list[bytes] = []   # recent raw reports (debug)
        self._last_labels: dict[str, set[str]] | None = None
        self._os_hits = [0, 0]                 # OS-confirmed convention hits

    def snapshot(self) -> set[str]:
        """Thread-safe copy of the current pressed-key set."""
        with self._lock:
            return set(self.latest)

    @property
    def convention(self) -> str:
        return {None: "auto", 0: "flat", 1: "spec"}[self._conv]

    def _decode_bits(self, kb: bytes) -> tuple[set[str], set[str], set[int]]:
        """Return (flat_labels, spec_labels, set_bit_indices)."""
        flat: set[str] = set()
        spec: set[str] = set()
        bits: set[int] = set()
        for bi in range(min(12, len(kb))):
            b = kb[bi]
            if not b:
                continue
            for bit in range(8):
                if (b >> bit) & 1:
                    idx = bi * 8 + bit
                    bits.add(idx)
                    lab = _USAGE2K.get(idx)              # flat: usage = idx
                    if lab:
                        flat.add(lab)
                    lab2 = _USAGE2K.get(idx + _CONV_SPEC_OFFSET)  # spec
                    if lab2:
                        spec.add(lab2)
        return flat, spec, bits

    def _update_convention(self, flat: set[str], spec: set[str],
                           prev: set[str]) -> None:
        """Learn which packing the firmware uses.

        A press transition (old -> new) adds the newly-appeared labels to the
        scoring of each convention.  Once one convention has strictly more
        hits AND the other has none, lock it in.
        """
        if self._conv is not None:
            return
        new_flat = flat - prev
        new_spec = spec - prev
        if new_flat:
            self._conv_hits[0] += 1
        if new_spec:
            self._conv_hits[1] += 1
        f, s = self._conv_hits
        if f > 0 and s == 0:
            self._conv = 0
        elif s > 0 and f == 0:
            self._conv = 1
        elif f >= 2 and f > s:
            self._conv = 0
        elif s >= 2 and s > f:
            self._conv = 1

    def observe(self, os_held: set[str]) -> None:
        """Feed OS-level held WASD keys (GetAsyncKeyState) to confirm the
        bit-packing convention with ground truth.

        While the OS says a key is held, the HID report must decode it:
        if only one convention's decode covers the OS set, that convention
        scores; a single unambiguous hit locks it in.  Thread-safe.
        """
        with self._lock:
            if self._conv is not None or not self._last_labels or not os_held:
                return
            flat, spec = self._last_labels["flat"], self._last_labels["spec"]
            f_match = os_held.issubset(flat)
            s_match = os_held.issubset(spec)
            # OS state is ground truth: a report that covers the OS-held set
            # under EXACTLY ONE convention confirms that convention — one
            # unambiguous confirmation is enough to lock.
            if f_match and not s_match:
                self._os_hits[0] += 1
                self._conv = 0
            elif s_match and not f_match:
                self._os_hits[1] += 1
                self._conv = 1
            # ambiguous (both) or neither: no signal

    def open(self) -> bool:
        if hidapi is None:
            return False
        try:
            self._dev = hidapi.device()
            self._dev.open_path(self.path)
            self._dev.set_nonblocking(True)
            self.opened = True
            return True
        except OSError:
            return False

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except OSError:
                    pass
                self._dev = None
            self.opened = False
            self.latest = set()

    def poll(self) -> set[str]:
        """Drain pending reports; return the current pressed-key set (locked).

        Handles both bit-packing conventions (see _decode_bits); the
        convention is auto-detected from the first real press transitions.
        """
        with self._lock:
            if self._dev is None:
                return self.latest
            while True:
                try:
                    rep = self._dev.read(16)
                except OSError:
                    return self.latest
                if not rep:
                    return self.latest
                d = rep[1:] if (len(rep) >= 14 and rep[0] == 1) else rep
                if len(d) >= 13:
                    self._raw_reports.append(bytes(rep))
                    if len(self._raw_reports) > 64:
                        self._raw_reports.pop(0)
                    flat, spec, _bits = self._decode_bits(d[1:13])
                    self._last_labels = {"flat": flat, "spec": spec}
                    self._update_convention(flat, spec, self.latest)
                    if self._conv == 0:
                        ks = flat
                    elif self._conv == 1:
                        ks = spec
                    else:
                        ks = flat | spec  # unknown: report both (superset)
                    self.latest = ks
                    return ks
                return self.latest

    # ------------------------------------------------------------------


class OsKeys:
    """OS-level WASD press watcher (Windows GetAsyncKeyState).

    Independent of the keyboard's HID bit packing — a bulletproof
    key-press detector for calibration / live auto-cal.  The OS keyboard
    driver produces these events from the same hardware reports the app
    reads, so nothing is lost by using them for *press detection*.
    Non-Windows platforms: `available` stays False (poll returns empty).
    Thread-safe: GetAsyncKeyState carries no per-device state.
    """
    # GetAsyncKeyState expects VIRTUAL-KEY codes (0x41-0x5A for letters),
    # NOT scancodes.  (Older build used scancodes 0x11/0x1E/0x1F/0x20, which
    # silently matched nothing.)
    _VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}

    def __init__(self):
        self.available = False
        self._user32 = None
        if sys.platform == "win32":
            try:
                import ctypes
                self._user32 = ctypes.windll.user32
                self.available = True
            except Exception:
                self.available = False

    def poll(self) -> set[str]:
        """Currently-held WASD keys per the OS (empty if unavailable)."""
        if not self.available:
            return set()
        out: set[str] = set()
        for k, vk in self._VK.items():
            try:
                if self._user32.GetAsyncKeyState(vk) & 0x8000:
                    out.add(k)
            except Exception:
                continue
        return out


def find_interfaces() -> dict[str, bytes]:
    """Return {'vendor': path, 'kbd': path} for the Slice75 HE."""
    out = {}
    if hidapi is None:
        return out
    for d in hidapi.enumerate():
        if d.get("vendor_id") == VID and d.get("product_id") == PID:
            p = d["path"]
            if b"MI_02" in p:
                out["vendor"] = p
            elif b"MI_00" in p:
                out["kbd"] = p
            elif b"MI_01" in p and b"Col01" in p:
                out["keys"] = p
    return out


@dataclass
class Mapping:
    """Maps logical keys -> data source.

    source 'vendor' uses the travel matrix position; 'hid' uses the
    6-byte analog block index.
    """
    keys: dict[str, str] = field(default_factory=lambda: {"W": "adc", "A": "adc", "S": "adc", "D": "adc"})
    vendor_pos: dict[str, int] = field(default_factory=dict)     # key -> matrix pos (legacy mm)
    adc_pos: dict[str, int] = field(default_factory=dict)        # key -> ADC sensor pos
    hid_byte: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_HID_MAP))

    # Ground-truth WASD -> ADC sensor positions, captured on this board by
    # correlating OS-confirmed keypresses with the raw Hall ADC stream
    # (FW 1.1.7.3).  pos = bank*ADC_CELLS_PER_HALF + idx (T1 base 0,
    # T2 base 61; idx 0..60 over the full 61-cell frame):
    #   W (T1, idx 44) = 44   [continuation report — the cell the
    #                          29-cell-only parser never read]
    #   A (T2, idx 1)  = 62
    #   S (T2, idx 2)  = 63
    #   D (T2, idx 3)  = 64
    DEFAULT_ADC_MAP = {"W": 44, "A": 62, "S": 63, "D": 64}

    def __post_init__(self):
        for k in ("W", "A", "S", "D"):
            self.adc_pos.setdefault(k, self.DEFAULT_ADC_MAP[k])
            self.keys.setdefault(k, "adc")

    def to_dict(self) -> dict:
        return {"keys": self.keys, "vendor_pos": self.vendor_pos,
                "adc_pos": self.adc_pos, "hid_byte": self.hid_byte}

    @classmethod
    def load(cls, path: str) -> "Mapping":
        if os.path.exists(path):
            try:
                with open(path) as f:
                    raw = json.load(f)
                m = cls()
                m.keys = {k: v for k, v in raw.get("keys", {}).items()}
                m.vendor_pos = {k: int(v) for k, v in raw.get("vendor_pos", {}).items()}
                m.adc_pos = {k: int(v) for k, v in raw.get("adc_pos", {}).items()}
                m.hid_byte = {k: int(v) for k, v in raw.get("hid_byte", {}).items()}
                for k in ("W", "A", "S", "D"):
                    m.keys.setdefault(k, "adc")
                    m.adc_pos.setdefault(k, cls.DEFAULT_ADC_MAP[k])
                    m.hid_byte.setdefault(k, DEFAULT_HID_MAP[k])
                return m
            except Exception:
                pass
        return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


def read_key_value(mapping: Mapping, key: str,
                   vendor: VendorStream | None,
                   hid: HidAnalog | None) -> float:
    """Normalized 0.0..1.0 travel for a logical key.

    vendor: mm / 3.3mm  (Jade Pro full travel, clamped)
    hid:    byte / 255
    """
    src = mapping.keys.get(key, "adc")
    # primary: raw Hall ADC travel (live, self-normalizing)
    if src == "adc" and vendor is not None and vendor.running:
        pos = mapping.adc_pos.get(key)
        if pos is None:
            pos = getattr(mapping, "DEFAULT_ADC_MAP", {}).get(key)
        if pos is not None:
            return vendor.travel(pos)
    # secondary: legacy 16-bit mm (only non-zero after a board-side cal)
    if src == "vendor" and vendor is not None and vendor.running:
        pos = mapping.vendor_pos.get(key)
        if pos is not None:
            with vendor._lock:
                mm = vendor.mm.get(pos)
            if mm is not None:
                return max(0.0, min(1.0, mm / 3300.0))
    # tertiary: the 6-byte HID analog block
    if hid is not None and hid.opened:
        b = mapping.hid_byte.get(key)
        if b is not None and 0 <= b <= 5:
            return hid.latest[b] / 255.0
    return 0.0
