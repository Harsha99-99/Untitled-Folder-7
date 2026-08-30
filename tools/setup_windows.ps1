# tools/setup_windows.ps1  — Windows-native setup (BLE + analysis)
#
# Sets up the cross-platform parts that run directly on Windows:
#   - BLE scan/connect (bleak)
#   - Signal analysis (numpy/scipy/matplotlib)
# The Classic-BT / SCO / HCI modules do NOT run on Windows — use Docker or a
# Linux host for those (see docker-compose.yml).

Write-Host "[*] Setting up Windows-native research environment..." -ForegroundColor Cyan

# Create a virtual environment
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "[+] Created .venv"
}

# Activate and install
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

# Create working directories
foreach ($d in @("data\captures", "data\analysis", "reports", "logs")) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

Write-Host "[+] Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Try a BLE scan (device you own, in pairing/advertising mode):"
Write-Host "  .\.venv\Scripts\python.exe src\ble_connect.py scan --timeout 12"
Write-Host ""
Write-Host "Then connect + enumerate GATT:"
Write-Host "  .\.venv\Scripts\python.exe src\ble_connect.py connect <ADDRESS>"
