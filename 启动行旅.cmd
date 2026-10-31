@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Project Python environment is missing. See README.md.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0scripts\desktop_launcher.py"
exit /b 0
