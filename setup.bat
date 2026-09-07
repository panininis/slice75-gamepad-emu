@echo off
rem One-time setup for Slice Pad (run as a normal user; ViGEm driver install
rem may prompt for UAC).
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Install it:  winget install astral-sh.uv
    pause
    exit /b 1
)
uv venv --python 3.12 .venv
set VGAMEPAD_SKIP_VIGEMBUS_INSTALL=1
rem Pinned deps from requirements.txt (hidapi, vgamepad, customtkinter) —
rem the single source of truth, so setup.bat can't drift from it.
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
rem Install the ViGEmBus kernel driver (virtual Xbox 360 bus).
echo Installing ViGEmBus driver (UAC prompt may appear)...
start "" msiexec /i "%~dp0.venv\Lib\site-packages\vgamepad\win\vigem\install\x64\ViGEmBusSetup_x64.msi" /qn /norestart
timeout /t 10 >nul
sc query ViGEmBus | findstr "RUNNING" >nul && echo ViGEmBus driver: RUNNING || echo ViGEmBus driver: not running yet (re-run setup or reboot)
echo Done. Start the app with:  run.bat
pause
endlocal
