# tunnel.ps1 — expose the web app on a public HTTPS URL via Cloudflare Tunnel.
#
# Gives a https://<random>.trycloudflare.com URL with a VALID cert (no browser
# warning), reachable from any device on any network. No Cloudflare account
# needed. Ctrl+C stops both the tunnel and the local server.
#
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Port 8000
#
# NOTE: the page still captures the mic of whichever DEVICE opens it — the
# tunnel changes reachability, not where audio is captured.

param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# locate cloudflared
$cf = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cf)) {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { $cf = $cmd.Source }
    else {
        Write-Host "[-] cloudflared not found."
        Write-Host "    Install:  winget install --id Cloudflare.cloudflared"
        exit 1
    }
}
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "[-] .venv missing. Run:  powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1"
    exit 1
}

Write-Host "[*] Starting hub on http://localhost:$Port (banner below shows the monitor URL + token)..."
$srv = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "hub.py", "--port", "$Port" `
    -PassThru -NoNewWindow
Start-Sleep -Seconds 2

Write-Host "[*] Opening Cloudflare tunnel (public HTTPS, valid cert)..."
Write-Host "    Watch for:  https://<name>.trycloudflare.com"
Write-Host "    Open that URL on any device. Ctrl+C to stop.`n"
try {
    & $cf tunnel --url "http://localhost:$Port" --no-autoupdate
}
finally {
    if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force }
    Write-Host "`n[*] Server + tunnel stopped."
}
