@echo off
REM Launch the multi-device hub (LAN use). For remote devices beyond your LAN,
REM use tunnel.ps1 instead (adds a public HTTPS URL).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [!] Run:  powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1
    pause
    exit /b 1
)
".venv\Scripts\python.exe" hub.py %*
