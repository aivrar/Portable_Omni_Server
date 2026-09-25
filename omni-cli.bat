@echo off
rem Windows shim for the Omni Studio CLI.
rem Routes through the WSL distro that hosts the gateway.
set "DISTRO=%OMNI_WSL_DISTRO%"
if "%DISTRO%"=="" set "DISTRO=linbox-Omni_Studio"
wsl -d "%DISTRO%" -- /usr/bin/env PYTHONPATH=/opt/omni_studio /opt/omni_studio/venv/bin/python3 -m cli.omni %*
