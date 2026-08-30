@echo off
REM Launch the Bluetooth Audio Monitor GUI (Windows)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [!] Virtual env not found. Run:  powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app.py
