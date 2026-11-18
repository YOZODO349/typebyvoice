@echo off
setlocal
cd /d "%~dp0"
if exist "stopped.flag" del /f /q "stopped.flag" >nul 2>nul

set "PY="
for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if not defined PY (
  echo.
  echo [x] python.exe not found. Install Python 3.10+ and tick "Add python.exe to PATH".
  echo     https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

rem launch.exe starts python.exe with CREATE_NO_WINDOW: no console window, no focus stealing.
if exist "%~dp0launch.exe" (
  "%~dp0launch.exe" "%~dp0." "%PY%" "%~dp0bridge.py"
  exit /b 0
)

powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%PY%' -ArgumentList '%~dp0bridge.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
exit /b 0
