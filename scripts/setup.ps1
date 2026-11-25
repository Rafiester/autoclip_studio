$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolsRoot = Join-Path $ProjectRoot 'tools'
$UvExe = Join-Path $ToolsRoot 'uv.exe'
$RuntimeRoot = Join-Path $ProjectRoot '.runtime'
$VenvRoot = Join-Path $ProjectRoot '.venv'
$FfmpegRoot = Join-Path $ToolsRoot 'ffmpeg'

New-Item -ItemType Directory -Force -Path $ToolsRoot | Out-Null

if (-not (Test-Path -LiteralPath $UvExe)) {
    Write-Host '[1/4] Downloading the project-local uv package manager...'
    $UvZip = Join-Path $env:TEMP ('autoclip-uv-' + [guid]::NewGuid().ToString('N') + '.zip')
    $UvExtract = Join-Path $env:TEMP ('autoclip-uv-' + [guid]::NewGuid().ToString('N'))
    Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $UvZip
    Expand-Archive -LiteralPath $UvZip -DestinationPath $UvExtract
    Copy-Item -LiteralPath (Join-Path $UvExtract 'uv.exe') -Destination $UvExe
    Remove-Item -LiteralPath $UvZip -Force
    Remove-Item -LiteralPath $UvExtract -Recurse -Force
} else {
    Write-Host '[1/4] uv is already installed.'
}

$env:UV_PYTHON_INSTALL_DIR = $RuntimeRoot
$env:UV_CACHE_DIR = Join-Path $ProjectRoot '.uv-cache'
if (Test-Path -LiteralPath (Join-Path $RuntimeRoot 'cache')) {
    Remove-Item -LiteralPath (Join-Path $RuntimeRoot 'cache') -Recurse -Force
}
Write-Host '[2/4] Installing an isolated Python 3.12 runtime...'
& $UvExe python install 3.12
if ($LASTEXITCODE -ne 0) { throw 'Unable to install the isolated Python runtime.' }
& $UvExe venv --python 3.12 --clear $VenvRoot
if ($LASTEXITCODE -ne 0) { throw 'Unable to create the virtual environment.' }

Write-Host '[3/4] Installing Python dependencies...'
& $UvExe pip install --python (Join-Path $VenvRoot 'Scripts\python.exe') -r (Join-Path $ProjectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

if (-not (Test-Path -LiteralPath (Join-Path $FfmpegRoot 'bin\ffmpeg.exe'))) {
    Write-Host '[4/4] Downloading a project-local FFmpeg build...'
    $FfmpegZip = Join-Path $env:TEMP ('autoclip-ffmpeg-' + [guid]::NewGuid().ToString('N') + '.zip')
    $FfmpegExtract = Join-Path $env:TEMP ('autoclip-ffmpeg-' + [guid]::NewGuid().ToString('N'))
    & curl.exe --fail --location --retry 2 --connect-timeout 20 --output $FfmpegZip 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $FfmpegZip).Length -lt 1000000) {
        Write-Host 'Primary FFmpeg mirror unavailable; using the GitHub release mirror...'
        & curl.exe --fail --location --retry 2 --connect-timeout 20 --output $FfmpegZip 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip'
    }
    if ($LASTEXITCODE -ne 0 -or (Get-Item -LiteralPath $FfmpegZip).Length -lt 1000000) { throw 'Unable to download FFmpeg.' }
    Expand-Archive -LiteralPath $FfmpegZip -DestinationPath $FfmpegExtract
    $ExtractedRoot = Get-ChildItem -LiteralPath $FfmpegExtract -Directory | Select-Object -First 1
    if (-not $ExtractedRoot) { throw 'The FFmpeg archive did not contain an expected directory.' }
    if (Test-Path -LiteralPath $FfmpegRoot) { Remove-Item -LiteralPath $FfmpegRoot -Recurse -Force }
    Move-Item -LiteralPath $ExtractedRoot.FullName -Destination $FfmpegRoot
    Remove-Item -LiteralPath $FfmpegZip -Force
    Remove-Item -LiteralPath $FfmpegExtract -Recurse -Force
} else {
    Write-Host '[4/4] FFmpeg is already installed.'
}

Write-Host ''
Write-Host 'Running the core test suite...'
& (Join-Path $VenvRoot 'Scripts\python.exe') -m pytest (Join-Path $ProjectRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Setup completed, but the validation tests failed.' }

Write-Host ''
Write-Host 'AutoClip Studio is ready. Launch it with START.bat.' -ForegroundColor Green

