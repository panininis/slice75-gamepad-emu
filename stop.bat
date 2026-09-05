@echo off
rem Slice Pad stopper - cleanly stops the web UI server (port 8321).
rem
rem The venv pythonw re-execs the real Windows python, so a single "server"
rem is actually TWO processes:
rem   parent  .venv\Scripts\pythonw.exe        (command line has the slice-pad path)
rem   child   WindowsApps\pythonw3.12.exe      (holds the port; command line has no slice-pad)
rem Killing the PORT OWNER is the authoritative, robust stop (a stale
rem command-line match can miss the child). We also sweep any leftover
rem app_web.py processes so a crashed double-launch can't leave a second
rem process fighting the keyboard's HID endpoint (which hangs the board ADC).
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$stop = $false; " ^
  "$c = Get-NetTCPConnection -LocalPort 8321 -State Listen -ErrorAction SilentlyContinue; " ^
  "if ($c) { foreach ($o in ($c.OwningProcess | Select-Object -Unique)) { Stop-Process -Id $o -Force -ErrorAction SilentlyContinue; Write-Host ('Stopped port owner pid ' + $o); $stop = $true } }; " ^
  "$strays = Get-CimInstance Win32_Process -Filter 'Name like ''python%''' | Where-Object { $_.CommandLine -like '*app_web.py*' }; " ^
  "foreach ($s in $strays) { Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('Swept stray pid ' + $s.ProcessId); $stop = $true }; " ^
  "if (-not $stop) { Write-Host 'No Slice Pad server running.' }"
echo.
echo [Slice Pad stopped.]
endlocal
