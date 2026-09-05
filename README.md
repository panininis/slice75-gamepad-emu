# Slice Pad — analog WASD → virtual Xbox 360 gamepad

Turns the **Chilkey Slice75 HE** Hall-Effect keyboard's analog WASD keys into a
virtual **Xbox 360 (XInput)** gamepad stick, with **diagonal vector
normalization** so moving diagonally (W+D) is not unnaturally fast.

```
run.bat          start the app (web UI on http://127.0.0.1:8321)
setup.bat        one-time setup: venv + deps + ViGEmBus driver
requirements.txt pinned Python deps (hidapi, vgamepad, customtkinter)
research_archive/  the protocol reverse-engineering scripts & captures
                   (61 probes/recorders) — kept for reference, not part
                   of the app
```

## What it does

- Reads the 6 analog key travels straight from the keyboard — **no key
  remapping, no extra drivers for the keyboard itself**.
  Two sources are fused and the stronger one wins per key (auto-selected by
  the calibration wizard):
  1. **Vendor real-time travel stream** (MI_02 vendor HID interface,
     usage page `0xFF00`): absolute per-key travel in 0.001 mm, the same
     data the official web driver's "real-time travel" page uses.
     Spoken via the keyboard's proprietary 65-byte HID packet protocol
     (head `0x5C`, checksum `53+b0+b1+b2+payload[len-1]`, CMD `18`
     = `KB2_CMD_RM6X21`, sub 2 = mm matrix, sub 3 = 8-bit press).
  2. **Standard HID analog block** (MI_00 keyboard report, bytes 2–7):
     6 × 8-bit (0–255) channels for the analog keys at the keyboard's
     native 8 kHz polling — used as fallback and for instant key-off.
- Feeds a **virtual Xbox 360 controller** (ViGEm/Nefarius) so *any*
  XInput game (or Steam's keyboard-and-mouse-to-controller) can drive a
  stick from your WASD keys.
- **Diagonal vector normalization**: the composed (x, y) stick vector is
  clamped to the unit circle (`√(x²+y²) ≤ 1`) — selectable:
  - *Full vector* (default): always clamp magnitude.
  - *Diagonal only*: normalize only when both axes are active.
  - *Off*: raw sum (each axis clamped to ±1).
- **Advanced shaping**: per-key deadzone, gain, and a **dynamic response
  curve** — any expression `f(v)` over the 0..1 travel (e.g.
  `v*v*(3-2*v)`, `sqrt(v)*0.6 + v*0.4`) with a live preview plot and
  presets (sandboxed compile; invalid expressions keep the previous
  curve), plus stick-level circular deadzone, left/right stick choice,
  axis inversion, optional W/S→LT/RT trigger drive, and a configurable
  max update rate.
- **Live GUI** (dark mode): stick ball with a hollow *ghost* showing the
  raw pre-normalization vector so you can see the fix working, 4 analog
  bars, telemetry, and key calibration two ways:
  - **live self-calibration** (default on): the first time each WASD key is
    held while the engine runs, the app observes which channel spikes,
    locks and persists the mapping, and announces it in the status line —
    the app is correct from the very first keystroke;
  - a manual **Calibrate now** wizard that walks through W→A→S→D.

## Requirements

- Windows 10/11 x64, USB connection (not Bluetooth).
- Python 3.10+ with [uv](https://docs.astral.sh/uv/) (or the bundled `.venv`).
- `pip` packages (see `requirements.txt`): `hidapi`, `vgamepad`,
  `customtkinter` (GUI only).
- **ViGEmBus** kernel driver (virtual gamepad bus) — installed by
  `setup.bat` from the MSI bundled with `vgamepad` (UAC prompt, no reboot).

> Close the official **Chilkey web driver** while Slice Pad is running —
> both open the vendor HID interface exclusively.

## The vendor protocol (reverse-engineered)

From the official web driver (`slice75.netlify.app`, Vue bundle
`driver_main.js`) and verified against the live board (firmware
`App V1.1.7.3`, serial `1732173217323721`):

| Direction | Wire format |
|---|---|
| Host → device | 65-byte HID output report: `[0x00 report-ID][64-byte payload]` |
| Device → host | 64-byte HID input report: `[0x5C head][len][class][checksum][d0][d1][data...]` (len 0x92 = 192 spans 3 reports) |

Command payload layout: `b0=0x5C, b1=len (bytes from b4 to first 0xFF),
b2=class (0x00 cmd / 0x01 sync), b3=(53+b0+b1+b2+b[3+len])&0xFF,
b4=command, b5..=params, then 0xFF 0xFF`.

Commands used:

| id | name | effect |
|---|---|---|
| 1  | `KB2_CMD_SYNC` (class 0x01) | handshake; response carries FW/app-mode ASCII |
| 18 | `KB2_CMD_RM6X21` | real-time travel: `(3,half)` arm, `(2,half)` mm matrix (16-bit LE, 0.001 mm), `(3,half)` press (8-bit), `(6,half)` raw ADC. `half=1` rows 0–2, `half=2` rows 3–5 (6×21 keys). Matrix starts at payload offset 6; sub-code at offset 5. |
| 41 | `KB2_CMD_DB` | global settings; response: global trigger travel (mm×1000 LE), press/release deadzones |

Device→host responses use `class = command + 128` (SYNC→0x81, travel→0x92,
DB→0xA9). A generic ACK frame `5C 04 80 14 …` confirms accepted commands.

HID interface map (VID `0x1CA3`, PID `0x0701`):

| Interface | Role |
|---|---|
| MI_00 | standard keyboard report (8 data bytes: mod, reserved, **6×8-bit analog**) |
| MI_01 Col01 | digital key matrix (12-byte HID keyboard) |
| MI_01 Col02/03/04 | consumer / system / mouse collections |
| MI_02 | vendor protocol channel (usage page `0xFF00`, 63/64-byte reports) |

## Layout

```
app/
  app.py            desktop GUI (customtkinter); drives the shared engine
  app_web.py        web UI server (http://127.0.0.1:8321, SSE) — the
                    default front-end via run.bat; also drives the shared
                    engine, so both front-ends behave identically
  engine_core.py    GamepadEngine — the shared WASD→gamepad engine (HID
                    open, 1 kHz poll loop, live auto-cal, calibration
                    wizard). UI-agnostic: GUI + web UI + tests all use it.
  applog.py         thread-safe logging (rotating file + console + crash hooks)
  slice_capture.py  HID access: vendor stream, HID analog block, digital keys,
                    vendor packet builder, mapping persistence
  gamepad_bridge.py virtual X360 pad, diagonal normalization, curves, deadzones
  mapping.json      adc_pos ground truth (W:44 A:62 S:63 D:64)
  smoke.py          GUI + math smoke test (no keys needed)
  logic_test.py     hardware-free logic tests (ring cache, restart guard,
                    config clamping, bridge math) — runs anywhere, no HID
  integration.py    end-to-end test (real HID + ViGEm, includes a live
                    restart/recovery check)
  web/index.html    single-page dashboard (OLED black, no deps)
run.bat  setup.bat  requirements.txt
```

Settings & mappings persist in `~/.slice-pad/` (`config.json`, `mapping.json`).

## Logging

- Log file: `~/.slice-pad/logs/slice-pad-<timestamp>.log` (rotating, 512 KB × 4).
  `~/.slice-pad/logs/latest.log` names the newest file.
- `run.bat` keeps the console open on a crash and prints the log path — a
  full traceback is always written to the log.
- Any unhandled exception (main thread **or** worker thread) is captured and
  logged with a full traceback. The engine loop never dies on a bad tick:
  it logs and retries on the next 1 ms cycle.

## Troubleshooting

- **"not found" on connect** — reconnect USB; check
  `Get-PnpDevice | ? InstanceId -match 1CA3`.
- **Dry-run badge** — ViGEmBus service missing: re-run `setup.bat` (UAC)
  or `sc query ViGEmBus`.
- **No stick movement** — press the *Calibrate now* button (needs the
  engine running); it waits for each WASD press and prints where the key
  was mapped. You can also edit `~/.slice-pad/mapping.json` by hand.
- **Keyboard stops responding** — close Slice Pad and restart the
  keyboard (the vendor OUT endpoint occasionally needs a power cycle).
- **Anti-cheat** — virtual controllers can be flagged by EAC/Vanguard/
  Ricochet; use in offline/single-player or accessibility contexts.
- **App crashed / froze** — check `~/.slice-pad/logs/latest.log` (path is
  printed in the console window). Engine-loop and calibration-worker errors
  are logged with tracebacks and never take the whole app down.

## Key-press detection

* Press detection uses the **OS keyboard state** (GetAsyncKeyState) as the
  ground-truth detector, because the Slice75 HE's MI_01 (digital keys)
  interface streams **no reports** while idle or during a press — verified
  live.  The OS events come from the same hardware reports, so nothing is
  lost.
* The MI_01 interface is still decoded defensively: HID *usage* values
  (W=0x1A, A=0x04, S=0x16, D=0x07 — the old code mistakenly used Set-1
  scancodes) under **both** bit-packing conventions, with automatic
  convention detection (OS-confirmed).  If the firmware ever starts
  streaming MI_01, it is used in addition to the OS state.
* Telemetry shows the detected convention and hit counts.

## Thread-safety

The 1 kHz engine loop, the calibration worker, and the UI thread share the
keyboard HID handles. All shared state is protected: each HID device is
read under its own lock, the vendor travel matrix and the 6-byte analog
block are consumed via thread-safe `snapshot()` copies, and the UI queue is
lock-guarded. Calibration scores the *rise from a pre-press baseline*, so a
key's leftover travel value can never be mis-attributed to another key.
