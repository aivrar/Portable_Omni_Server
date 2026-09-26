@echo off
rem Windows shim for the Omni Studio CLI.
rem PowerShell passes the distro as an argument without unnecessary literal quotes.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0omni-cli.ps1" %*
exit /b %ERRORLEVEL%
