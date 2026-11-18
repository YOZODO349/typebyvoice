@echo off
rem Stop TypeByVoice: leaves a flag so the watchdog exits instead of restarting it.
setlocal
cd /d "%~dp0"
echo stopped > "%~dp0stopped.flag"
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /C:":8765" ^| findstr /C:"LISTENING"') do taskkill /f /pid %%p >nul 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*watchdog.vbs*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
echo TypeByVoice stopped. Double-click run.bat to start it again.
timeout /t 2 >nul
