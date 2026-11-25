param([switch]$Installer, [string]$IsccPath)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$ExePath = Join-Path $Root 'dist\AutoClipStudio\AutoClipStudio.exe'
if (Get-Process -Name AutoClipStudio -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $ExePath }) {
    throw 'Close the existing OneDir executable before rebuilding.'
}
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Uv = Join-Path $Root 'tools\uv.exe'
if (!(Test-Path -LiteralPath $Python) -or !(Test-Path -LiteralPath $Uv) -or
    !(Test-Path -LiteralPath (Join-Path $Root 'tools\ffmpeg\bin\ffmpeg.exe')) -or
    !(Test-Path -LiteralPath (Join-Path $Root 'tools\ffmpeg\bin\ffprobe.exe'))) {
    & (Join-Path $Root 'scripts\setup.ps1')
}
$env:UV_CACHE_DIR = Join-Path $Root '.uv-cache'
& $Uv pip install --python $Python -r requirements.txt -r (Join-Path $PSScriptRoot 'requirements-build.txt')
if ($LASTEXITCODE) { throw 'Could not install build dependencies.' }

function Get-VerifiedDownload([string]$Url, [string]$Destination, [string]$Sha256) {
    if ((Test-Path -LiteralPath $Destination) -and
        ((Get-FileHash -LiteralPath $Destination).Hash -eq $Sha256)) { return }
    $Temporary = "$Destination.part"
    & curl.exe --fail --location --silent --show-error --retry 2 --connect-timeout 20 --max-time 600 --output $Temporary $Url
    if ($LASTEXITCODE -or (Get-FileHash -LiteralPath $Temporary).Hash -ne $Sha256) {
        throw "Download or SHA-256 verification failed for $Url"
    }
    Move-Item -LiteralPath $Temporary -Destination $Destination -Force
}

$Helpers = Join-Path $Root 'tools\package'
New-Item -ItemType Directory -Path $Helpers -Force | Out-Null
Get-VerifiedDownload 'https://github.com/yt-dlp/yt-dlp/releases/download/2026.11.23/yt-dlp.exe' (Join-Path $Helpers 'yt-dlp.exe') '622ed48023cac1daa52a3bf3a6ac38f03514de214b5f0fa069389fd0f6d17e5c'
$DenoZip = Join-Path $Helpers 'deno.zip'
Get-VerifiedDownload 'https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-pc-windows-msvc.zip' $DenoZip 'cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf'
Expand-Archive -LiteralPath $DenoZip -DestinationPath $Helpers -Force
$DenoLicense = Join-Path $Helpers 'DENO-LICENSE.md'
if (!(Test-Path -LiteralPath $DenoLicense)) {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 'https://raw.githubusercontent.com/denoland/deno/v2.9.8/LICENSE.md' -OutFile $DenoLicense
}
& $Python (Join-Path $PSScriptRoot 'make_icon.py') build/assets/autoclip.ico
if ($LASTEXITCODE) { throw 'Could not generate the application icon.' }
& $Python -m PyInstaller --noconfirm (Join-Path $PSScriptRoot 'AutoClipStudio.spec')
if ($LASTEXITCODE) { throw 'PyInstaller build failed.' }

if ($Installer) {
    if (!$IsccPath) {
        $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        $IsccPath = @(
            $Command.Source
            "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe"
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
            "$env:ProgramFiles\Inno Setup 7\ISCC.exe"
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe"
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    }
    if (!$IsccPath) { throw 'Inno Setup compiler unavailable. Supply -IsccPath with the location of ISCC.exe.' }
    & $IsccPath (Join-Path $PSScriptRoot 'AutoClipStudio.iss')
    if ($LASTEXITCODE) { throw 'Inno Setup compilation failed.' }
}
