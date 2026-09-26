# Windows entry point for the CLI in the registered Linux distro.
$ErrorActionPreference = 'Stop'
$omniCliDistro = $env:OMNI_WSL_DISTRO
if ([string]::IsNullOrWhiteSpace($omniCliDistro)) {
    $omniCliDistro = 'linbox-Omni_Studio'
}
& "$env:SystemRoot\System32\wsl.exe" -d $omniCliDistro -- /usr/bin/env `
    'PYTHONPATH=/opt/omni_studio' '/opt/omni_studio/venv/bin/python3' `
    -m cli.omni @args
exit $LASTEXITCODE
