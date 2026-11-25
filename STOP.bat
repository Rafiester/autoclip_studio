@echo off
setlocal
cd /d "%~dp0"
if not exist ".autoclip.pid" (
  echo AutoClip Studio is not running, or it has already exited.
  exit /b 0
)
set /p AUTOCLIP_PID=<".autoclip.pid"
powershell.exe -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $env:AUTOCLIP_PID) -ErrorAction SilentlyContinue; if ($p -and $p.CommandLine -like '*main.py*--autoclip-studio*') { & taskkill.exe /PID $p.ProcessId /T /F | Out-Null; exit $LASTEXITCODE } elseif (-not $p) { exit 0 } else { Write-Error 'PID belongs to another process; refusing to stop it.'; exit 2 }"
if errorlevel 1 exit /b 1
del /q ".autoclip.pid" 2>nul
echo AutoClip Studio processes stopped.

