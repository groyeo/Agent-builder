@echo off
rem One-click dev environment for Agent Builder (backend + frontend).
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [!] python not found. Install Python 3.10+ and check "Add to PATH".
  pause
  exit /b 1
)
python start.py %*
pause
