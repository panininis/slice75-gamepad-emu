@echo off
rem Slice Pad launcher — runs the WASD->gamepad app with the project venv.
rem -u = unbuffered output so the live log (INFO lines) appears below.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv — run setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -u app\app_web.py
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo [Slice Pad exited with code %RC% — full details in the log file:]
    type "%USERPROFILE%\.slice-pad\logs\latest.log" 2>nul
    echo.
    pause
)
endlocal
