@echo off
rem Double-clickable wrapper for build_portable_offline.ps1: builds the
rem offline assets package, then the portable zip. Passes any arguments
rem through to the PowerShell script.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_portable_offline.ps1" %*
if errorlevel 1 (
  echo.
  echo Build failed, see the messages above.
  pause
  exit /b 1
)
echo.
pause
