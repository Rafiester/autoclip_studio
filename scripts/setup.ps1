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

if (!(Test-Path -LiteralPath (Join-Path $FfmpegRoot 'bin\ffmpeg.exe')) -or
    !(Test-Path -LiteralPath (Join-Path $FfmpegRoot 'bin\ffprobe.exe'))) {
    Write-Host '[4/4] Downloading a project-local FFmpeg build...'
    $FfmpegZip = Join-Path $env:TEMP ('autoclip-ffmpeg-' + [guid]::NewGuid().ToString('N') + '.zip')
    $FfmpegExtract = Join-Path $env:TEMP ('autoclip-ffmpeg-' + [guid]::NewGuid().ToString('N'))
    $FfmpegRelease = 'ffmpeg-9.1-essentials_build.zip'
    $FfmpegSha = 'b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd'
    $Backup = $null
    try {
        & curl.exe --fail --location --retry 2 --connect-timeout 20 --max-time 600 --output $FfmpegZip "https://github.com/GyanD/codexffmpeg/releases/download/9.1/$FfmpegRelease"
        if ($LASTEXITCODE -ne 0) {
            & curl.exe --fail --location --retry 2 --connect-timeout 20 --max-time 600 --output $FfmpegZip "https://www.gyan.dev/ffmpeg/builds/packages/$FfmpegRelease"
        }
        if ($LASTEXITCODE -ne 0) { throw 'Unable to download FFmpeg.' }
        if ((Get-FileHash -LiteralPath $FfmpegZip -Algorithm SHA256).Hash -ne $FfmpegSha) {
            throw 'The FFmpeg archive did not match the pinned SHA-256 checksum.'
        }
        Expand-Archive -LiteralPath $FfmpegZip -DestinationPath $FfmpegExtract
        $ExtractedRoot = Join-Path $FfmpegExtract 'ffmpeg-9.1-essentials_build'
        if (!(Test-Path -LiteralPath (Join-Path $ExtractedRoot 'bin\ffmpeg.exe')) -or
            !(Test-Path -LiteralPath (Join-Path $ExtractedRoot 'bin\ffprobe.exe'))) {
            throw 'The FFmpeg archive did not contain both required executables.'
        }
        if (Test-Path -LiteralPath $FfmpegRoot) {
            $Backup = Join-Path $ToolsRoot ('ffmpeg-backup-' + [guid]::NewGuid().ToString('N'))
            Move-Item -LiteralPath $FfmpegRoot -Destination $Backup
        }
        try {
            Move-Item -LiteralPath $ExtractedRoot -Destination $FfmpegRoot
        } catch {
            if ($Backup -and !(Test-Path -LiteralPath $FfmpegRoot)) {
                Move-Item -LiteralPath $Backup -Destination $FfmpegRoot
            }
            throw
        }
    } finally {
        if (Test-Path -LiteralPath $FfmpegZip) { Remove-Item -LiteralPath $FfmpegZip -Force }
        if (Test-Path -LiteralPath $FfmpegExtract) {
            $ResolvedExtract = (Resolve-Path -LiteralPath $FfmpegExtract).Path
            $ResolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path.TrimEnd('\')
            if ($ResolvedExtract.StartsWith("$ResolvedTemp\", [StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item -LiteralPath $ResolvedExtract -Recurse -Force
            }
        }
    }
} else {
    Write-Host '[4/4] FFmpeg is already installed.'
}
& (Join-Path $FfmpegRoot 'bin\ffmpeg.exe') -version | Select-Object -First 1
if ($LASTEXITCODE -ne 0) { throw 'The project-local FFmpeg executable could not start.' }
& (Join-Path $FfmpegRoot 'bin\ffprobe.exe') -version | Select-Object -First 1
if ($LASTEXITCODE -ne 0) { throw 'The project-local ffprobe executable could not start.' }

Write-Host ''
Write-Host 'Running the core test suite...'
& (Join-Path $VenvRoot 'Scripts\python.exe') -m pytest (Join-Path $ProjectRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Setup completed, but the validation tests failed.' }

Write-Host ''
Write-Host 'AutoClip Studio is ready. Launch it with START.bat.' -ForegroundColor Green

