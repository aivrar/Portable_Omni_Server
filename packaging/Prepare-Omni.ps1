param(
    [string]$PartsDirectory = (Join-Path $PSScriptRoot 'linux\parts'),
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json') -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1 -or $manifest.tag -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') {
    throw 'Unsupported release manifest.'
}
if ($manifest.image.sha256 -notmatch '^[a-f0-9]{64}$' -or $manifest.image.size -le 0 -or @($manifest.image.parts).Count -eq 0) {
    throw 'Invalid image manifest.'
}
$linux = Join-Path $PSScriptRoot 'linux'
$target = Join-Path $linux 'rootfs.tar.gz'
New-Item -ItemType Directory -Path $linux -Force | Out-Null
if (Test-Path -LiteralPath $target) {
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -eq $manifest.image.sha256) {
        Write-Host 'The Linux image is ready. Launch Omni_Studio.exe.'
        exit 0
    }
    throw 'linux\rootfs.tar.gz already exists with different contents. Move it aside before preparing this release.'
}
New-Item -ItemType Directory -Path $PartsDirectory -Force | Out-Null
$baseUrl = 'https://github.com/aivrar/Portable_Omni_Server/releases/download/' + $manifest.tag
$seen = @{}
[long]$totalSize = 0
foreach ($part in $manifest.image.parts) {
    if ($part.name -notmatch '^omni-rootfs-v[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz\.[0-9]{3}$' -or
        $part.sha256 -notmatch '^[a-f0-9]{64}$' -or $part.size -le 0 -or $seen.ContainsKey($part.name)) {
        throw 'Invalid or duplicate image part in manifest.'
    }
    $seen[$part.name] = $true
    $totalSize += [long]$part.size
}
if ($totalSize -ne [long]$manifest.image.size) { throw 'Image part sizes do not match the image size.' }
Add-Type -AssemblyName System.Net.Http
$client = New-Object System.Net.Http.HttpClient
$client.Timeout = [TimeSpan]::FromHours(8)
try {
    foreach ($part in $manifest.image.parts) {
        $path = Join-Path $PartsDirectory $part.name
        if (Test-Path -LiteralPath $path) {
            if ((Get-Item -LiteralPath $path).Length -eq [long]$part.size -and
                (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $part.sha256) {
                Write-Host ('Verified cached ' + $part.name)
                continue
            }
            throw ('Existing part has an incorrect checksum: ' + $path + '. Move it aside and retry.')
        }
        if ($Offline) { throw ('Missing image part: ' + $path) }
        Write-Host ('Downloading ' + $part.name + ' ...')
        $partial = $path + '.download'
        $response = $client.GetAsync($baseUrl + '/' + $part.name, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        try {
            $response.EnsureSuccessStatusCode() | Out-Null
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = [IO.File]::Open($partial, [IO.FileMode]::Create, [IO.FileAccess]::Write)
            try { $inputStream.CopyTo($outputStream, 1048576) }
            finally { $outputStream.Dispose(); $inputStream.Dispose() }
        } finally { $response.Dispose() }
        if ((Get-Item -LiteralPath $partial).Length -ne [long]$part.size -or
            (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $part.sha256) {
            throw ('Downloaded part failed verification: ' + $part.name + '. Retry to download it again.')
        }
        Move-Item -LiteralPath $partial -Destination $path
    }
} finally { $client.Dispose() }
$assembled = Join-Path $linux ('rootfs.' + [Guid]::NewGuid().ToString('N') + '.assembling')
Write-Host 'Assembling the preinstalled Linux image ...'
$outputStream = [IO.File]::Open($assembled, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
try {
    foreach ($part in $manifest.image.parts) {
        $inputStream = [IO.File]::OpenRead((Join-Path $PartsDirectory $part.name))
        try { $inputStream.CopyTo($outputStream, 1048576) }
        finally { $inputStream.Dispose() }
    }
} finally { $outputStream.Dispose() }
if ((Get-Item -LiteralPath $assembled).Length -ne [long]$manifest.image.size -or
    (Get-FileHash -LiteralPath $assembled -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.image.sha256) {
    throw ('Assembled image failed verification. Preserve this file for diagnosis: ' + $assembled)
}
Move-Item -LiteralPath $assembled -Destination $target
Write-Host 'Ready. Launch Omni_Studio.exe. Model weights are downloaded separately inside the app.'
Write-Host 'The verified parts in linux\parts may be removed after successful first launch to reclaim space.'
