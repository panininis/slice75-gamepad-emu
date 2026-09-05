# Slice Pad — codebase analysis & optimization (2026-09-05)

Full review of `app/` (the production code). Production is ~4,100 lines across
8 Python files + one no-dependency HTML UI. Research artifacts (61
probe/recorder scripts + captures) were archived out of the way.

## What was changed

### 1. Crash fix — desktop app wouldn't launch (was blocking SMOKE)
`app.py` referenced **6 methods that didn't exist** (`_apply_chrome`,
`_force_focus`, `_toggle_max`, `_minimize`, `_drag_start`, `_drag_move`), so the
frameless window crashed 30 ms after start. Implemented all six.
**Verified: SMOKE PASS.**

### 2. Big refactor — desktop app now shares the engine
`app.py` (tkinter) had re-implemented the **entire engine** (open/HID, 1 kHz
loop, live auto-cal, baseline ratchet, calibration wizard) as a second copy of
`engine_core.GamepadEngine` — ~450 lines that had already drifted apart (the
shared copy has the `_starting` guard, `live_autocal` cfg check, and `stats`;
the desktop copy didn't). Two engines = every bug fixed twice.

Now `app.py` **drives the shared `GamepadEngine`** — the same class the web UI
uses — so both front-ends behave identically and bugs are fixed in one place.
GUI state is read through read-through properties (`self.vendor`,
`self.bridge`, `self.running`, `self.stats`, …). Engine start/stop now run on
worker threads so the ~1.4 s interface-open no longer blocks the UI, and a new
`_update_engine_ui()` gives the button its 3-state (idle / connecting /
running) behaviour.

- `app.py`: **1197 → 842 lines** (−355).
- **Verified: SMOKE + INTEGRATION PASS on live hardware** (W forced → y16=32767,
  parser round-trip trW=0.962).

### 3. Correctness — auto-calibration trigger was dead
`engine_core._engine_loop` triggered the interactive auto-cal with
`or not self.mapping.vendor_pos` — but a legacy HID calibration leaves
`vendor_pos` non-empty, so the condition was **always true** and the interactive
wizard never fired. Now it keys off the real signal: "no `adc_pos` analog
mapping saved yet".

### 4. Correctness — Win32 timer resolution for the 1 kHz loop
The engine's `time.sleep(0.001)` "1 kHz" loop relied on Windows' default
**~15.6 ms** timer resolution. Now wraps the loop in `timeBeginPeriod(1)` /
`timeEndPeriod(1)` so the 1 ms sleeps are actually ~1 ms. (No-op on failure /
non-Windows.)

### 5. Perf — hot-path O(n) list ops at ~660 fps
`slice_capture`'s per-sensor rolling baseline used
`list.append` + `list.pop(0)` — an **O(n) memmove of 300 pointers per cell per
frame** (~50k memmoves/sec). Replaced with `deque(maxlen=300)` (O(1)
append/evict). **Identical math** (seeded median + upward ratchet) — verified
with a synthetic `travel()` test (rest 0.0, press 0.96) plus live INTEGRATION.

### 6. Perf — SSE lock contention in the web server
`Hub.snapshot()` held the hub lock while doing `json.loads(json.dumps(...))`.
The SSE loop calls `snapshot()` at 30 Hz and the engine's 1 kHz `emit()` blocks
on that same lock — so the hot path was **waiting on JSON serialization over
the network**. `snapshot()` now deep-copies under the lock and serializes
outside it.

### 7. Exit-hang in SMOKE
`smoke.py` opened three virtual pads and never closed them; unclosed ViGEm
pads hang the COM teardown, so the process lived on after "SMOKE PASS". Now
closes them. SMOKE exits 0 cleanly.

## What was cleaned

- **Root clutter:** 61 research probes/recorders + 25 capture JSON/JSONL + 46
  stray logs moved to `research_archive/` (confirmed **none** are imported by
  `app/` or referenced by `run.bat`). Root is now `app/`, `README`, `run.bat`,
  `setup.bat`, `requirements.txt`, `research_archive/`.
- **`requirements.txt`** (was missing — deps were only in `setup.bat`):
  accurate pinned set. `numpy` was installed but **never imported** by
  production code, so it's deliberately not listed.
- **`README.md`**: corrected the layout (added `engine_core.py`,
  `app_web.py`, `mapping.json`, `web/index.html`), fixed stale refs to moved
  files, removed `numpy`, added `requirements.txt`.
- **Git**: the repo had **no version control**. Initialized git; baseline +
  each change is a commit (rollback-safe).

## What was deliberately NOT changed

- **`slice_capture.py` protocol logic** — the 61-cell frame parser, bank
  classification, self-calibrating baseline ratchet, stream-dead watchdog, and
  board-hang mitigations are the carefully reverse-engineered crown jewel.
  Only the hot data-structure (list→deque) and nothing else was touched.
- **UI look & feel** — the OLED-black, no-glow, red-curve, dirty-check
  dashboard was the result of many explicit user requests; left untouched.
- **`app.py` GUI internals** — only the engine wiring was replaced; the
  customtkinter widget tree, bars, and stick visualizer are unchanged.

## Second pass (2026-09-05, ~09:00–10:30) — perf + recovery

### 8. Perf — `travel()` rescanned the 300-sample ring on every call
The engine calls `read_key_value` → `vendor.travel()` 4× per tick. `travel()`
did `max(ring)` over the **300-sample deque** every call — ~1.2M comparisons/s
at 1 kHz, even though each ring only changes at the ~124 Hz ADC frame rate.
Now `max(ring)` is cached and invalidated by a per-sensor generation counter
(bumped in `_process_92` when a sample lands) → O(1) between frames.
**Verified: cached == naive over 2000 mixed samples + full
seed/baseline/press/release `travel()` path.**

### 9. Perf — engine loop no longer polls blindly at 1 kHz
The loop now paces adaptively: **1 ms while a key is held or the stick is
off-center** (interaction), **~8 ms (~125 Hz, the ADC frame rate) when idle**
(no new analog data can arrive faster; an idle stick is 0 by definition).
A press is caught within one tick (≤1 ms active, ≤~8 ms from idle). Idle CPU
falls ~8× with no measurable responsiveness hit (a native Xbox pad refreshes
at 64–125 Hz).

### 10. Perf — refcounted 1 ms Win32 timer, shared by both loops
The sub-ms sleeps in the vendor ADC loop previously only got 1 ms resolution
while the engine loop was running (it owned the only `timeBeginPeriod`).
Now `slice_capture` exposes `timer_res_begin()/end()` (refcounted); both the
ADC loop and the engine loop hold a reference, so timer resolution is correct
whichever is running and the system timer is restored when both stop.

### 11. Recovery — one-click ADC stream recovery (was: "unplug & replug")
The firmware's RM6X21 task can hang (0x92 frames stop, control plane still
answers). The only known revival is **reopening the vendor endpoint** (verified
during research — replugging restores the stream, settings retained). An engine
stop+start is exactly a reopen, so `GamepadEngine.restart()` now does it in
place, and the web UI surfaces it: when `stream_dead` the engine button becomes
**"Recover ADC Stream"** (amber); one click runs `/api/restart` and the stream
comes back **without touching the USB cable**.
**Verified live: INTEGRATION restart check — fresh vendor stream
182 → 303 frames in 1 s, `stream_dead=False` after restart.**

### 12. Cosmetic — raw ghost now draws ON TOP of the ball
The dotted raw-travel ghost was drawn *before* the stick ball, so the ball's
22 px cyan glow hid it — making the 0.02 stick-deadzone gap look bigger than
it is (it's ~2 px at most, same-tick data, no time lag). The ghost is now
drawn after the ball (flat, slightly brighter), so the gap is visible but not
exaggerated.

### 13. Housekeeping
- **numpy uninstalled** from the venv (installed but never imported;
  already excluded from `requirements.txt`).
- **`snapshot()` deep-copy measured**: 7.9 µs/call → **0.024% of a core** at
  30 Hz. Negligible — left as-is (optimizing would risk correctness for
  nothing); documented here so it isn't "fixed" needlessly.
- `integration.py` gained the live restart check above.
- Scratch profiling script untracked (`.gitignore`).

## Measured result

Live web server (real HID + ViGEm pad, engine running):
- **CPU: 8.9% of one core over 90 s** (steady state, new-code sample via
  `Get-Process.TotalProcessorTime` delta). The earlier 12.6% figure was a
  15 s burst that included warm-up; the earlier "~0%" 30 s sample caught a
  low-activity window. 8.9% is the honest sustained number, and it is the
  combined cost of the ~660/s ADC reader thread (board-limited) + the engine
  loop (now 125 Hz idle) + SSE.
- **RAM: ~42 MB**, exactly one clean process on port 8321.
- Engine healthy: 662+ pad updates at last check, `stream_dead=False`,
  `dry_run=False`, firmware `App v1.1.7.3`.

## Test status
- SMOKE: **PASS** (exits 0)
- INTEGRATION: **PASS** on live hardware (W → y16=32767, parser trW=0.962,
  **restart recovery verified: fresh stream 182 → 303 frames/1 s**)
