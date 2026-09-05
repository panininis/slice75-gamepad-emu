"""Slice Pad logging.

Two destinations:
  * rotating file in ~/.slice-pad/logs (full history, DEBUG level)
  * console (INFO level) so run.bat shows what is happening

`log()` is safe to call from ANY thread.  install_hooks() captures
unhandled exceptions (main thread + worker threads) so a crash is
always written to the log file before the process exits.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from logging import Formatter as _Formatter
from logging.handlers import RotatingFileHandler
from logging import getLogger as _getLogger

APP_DIR = os.path.join(os.path.expanduser("~"), ".slice-pad")
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"slice-pad-{_ts}.log")
LATEST = os.path.join(LOG_DIR, "latest.log")

_logger = _getLogger("slicepad")
_logger.setLevel(10)
_logger.propagate = False
_fmt = _Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
                  datefmt="%H:%M:%S")

_fh = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=3,
                          encoding="utf-8")
_fh.setLevel(10)
_fh.setFormatter(_fmt)
_logger.addHandler(_fh)

if sys.stdout is not None:
    import logging as _logging
    _ch = _logging.StreamHandler(sys.stdout)
    _ch.setLevel(_logging.INFO)
    _ch.setFormatter(_fmt)
    _logger.addHandler(_ch)

_lock = threading.Lock()


def log(msg: str, level: str = "INFO") -> None:
    """Thread-safe log line (never raises)."""
    try:
        with _lock:
            _logger.log(int({"DEBUG": 10, "INFO": 20, "WARN": 30,
                             "ERROR": 40}.get(level, 20)), msg)
    except Exception:
        pass


def log_exception(where: str, exc: BaseException) -> None:
    try:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        log(f"[{where}] EXCEPTION {type(exc).__name__}: {exc}\n{''.join(tb)}",
            "ERROR")
    except Exception:
        pass


# ------------------------------------------------------------------ hooks
def install_hooks() -> None:
    """Capture unhandled exceptions in the main thread and worker threads.

    Main-thread hook: show the message (so the window does not just die
    silently), then exit(1) so run.bat keeps the console open.
    Worker-thread hook: log loudly, keep the process alive.
    """
    import sys as _s

    def _main_hook(exc_type, exc, tb):
        log_exception("main-thread", exc)
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            root = _tk._default_root  # type: ignore[attr-defined]
            if root is not None:
                _mb.showerror("Slice Pad crashed",
                              f"An unexpected error occurred (details in log "
                              f"file).\n\n{type(exc).__name__}: {exc}\n\n"
                              f"Log: {LATEST}")
        except Exception:
            pass
        _s.stderr.write(f"\n[crash] {type(exc).__name__}: {exc} "
                        f"(full trace in {LOG_FILE})\n")
        _s.stderr.flush()
        _s.exit(1)

    def _thread_hook(args):
        log_exception(f"thread {args.thread if hasattr(args, 'thread') else '?'}",
                      args.exc_value)
        # keep running; the engine loop retries each tick

    _s.excepthook = _main_hook
    threading.excepthook = _thread_hook
    log(f"log file: {LOG_FILE}")


def touch_latest() -> None:
    """Symlink/copy-free convenience: write a tiny marker so users know
    which log is newest (Windows has no cheap symlink, so we just write
    the current log path into latest.log)."""
    try:
        with open(LATEST, "w", encoding="utf-8") as f:
            f.write(LOG_FILE + "\n")
    except Exception:
        pass


__all__ = ["log", "log_exception", "install_hooks", "touch_latest",
           "LOG_FILE", "LATEST", "APP_DIR"]
