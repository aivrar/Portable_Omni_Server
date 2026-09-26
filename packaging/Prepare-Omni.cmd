@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Prepare-Omni.ps1" %*
if errorlevel 1 (
  echo.
  echo Preparation failed. Read the error above, then retry.
  pause
  exit /b 1
)
pause
