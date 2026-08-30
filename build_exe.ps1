# build_exe.ps1 - package the operator console as a standalone Windows .exe.
#
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Produces dist\BluetoothAudioHub.exe (onefile, windowed). The webapp/ folder
# is bundled so remote devices can still load the sender page from the exe.
# First run may take a few minutes and the exe is large (~150-250 MB) because
# it embeds numpy/scipy/aiohttp/PortAudio.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "[-] Run tools\setup_windows.ps1 first."; exit 1 }

Write-Host "[*] Installing PyInstaller..."
& $py -m pip install --upgrade pyinstaller | Out-Null

Write-Host "[*] Building (this can take a few minutes)..."
$iconArg = @()
if (Test-Path "assets\icon.ico") { $iconArg = @("--icon", "assets\icon.ico") }

& $py -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name BluetoothAudioHub `
    @iconArg `
    --add-data "webapp;webapp" `
    --collect-submodules aiohttp `
    --collect-data sounddevice `
    --hidden-import scipy.signal `
    hub_app.py

if (Test-Path "dist\BluetoothAudioHub.exe") {
    Write-Host "`n[+] Built: dist\BluetoothAudioHub.exe" -ForegroundColor Green
    Write-Host "    Double-click to run. Recordings + settings go next to the exe."
} else {
    Write-Host "[-] Build did not produce the exe - check the PyInstaller output above."
}
