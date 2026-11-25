@echo off
setlocal
cd /d "%~dp0"
title AutoClip Studio Setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 (
  echo.
  echo Setup failed. Review the error above, then run SETUP.bat again.
  pause
  exit /b 1
)
pause

