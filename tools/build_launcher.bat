@echo off
rem Build launch.exe from tools\launch.cs.
rem Why: run.bat / watchdog.vbs must start python.exe (console app) without any
rem console window. launch.exe is a tiny win-exe launcher that uses
rem CreateNoWindow, so nothing flashes on screen and focus is never stolen.
setlocal
cd /d "%~dp0.."
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo [x] csc.exe not found ^(.NET Framework 4 is required^).
  echo     You can still run run.bat - it falls back to PowerShell Start-Process -WindowStyle Hidden.
  pause
  exit /b 1
)
"%CSC%" /nologo /target:winexe /optimize /out:"%~dp0..\launch.exe" "%~dp0launch.cs"
if errorlevel 1 (
  echo [x] build failed.
  pause
  exit /b 1
)
echo [ok] launch.exe built next to bridge.py
