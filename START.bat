@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo AutoClip Studio is not installed. Run SETUP.bat first.
  pause
  exit /b 1
)
start "AutoClip Studio" /b ".venv\Scripts\pythonw.exe" "%~dp0main.py" --autoclip-studio
exit /b 0

