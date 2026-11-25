@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\build.ps1" %*
exit /b %errorlevel%
