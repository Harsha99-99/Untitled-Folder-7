# Hosting the web app beyond your LAN

The web app needs an **HTTPS origin with a valid cert** so `getUserMedia` works
on any device with no warning. A tunnel gives you that by exposing your local
`serve.py` through a public HTTPS URL.

> Reachability only — the page still captures the mic of whichever **device
> opens it**. Opening the public URL on a phone captures that phone's paired
> Bluetooth mic. It does not stream your PC's mic to visitors.

## Recommended: Cloudflare Tunnel (no account, valid cert)

`cloudflared` is already installed (`winget install --id Cloudflare.cloudflared`).
One command does everything:

```powershell
powershell -ExecutionPolicy Bypass -File tunnel.ps1
```

It starts the local server and prints a line like:

```
https://prior-toys-favors-telecharger.trycloudflare.com
```

Open that on any device, anywhere. Valid Cloudflare cert → **no warning** →
secure context → mic capture works. `Ctrl+C` stops both.

Manual equivalent (two terminals):

```powershell
.\.venv\Scripts\python.exe serve.py --http --port 8000
cloudflared tunnel --url http://localhost:8000
```

**Quick tunnels get a new random URL each run.** For a **stable custom domain**,
create a named tunnel (needs a free Cloudflare account + a domain on Cloudflare):

```powershell
cloudflared login
cloudflared tunnel create btmic
cloudflared tunnel route dns btmic btmic.yourdomain.com
cloudflared tunnel run --url http://localhost:8000 btmic
```

## Alternatives

**ngrok** (needs a free account + authtoken):

```powershell
winget install --id Ngrok.Ngrok
ngrok config add-authtoken <YOUR_TOKEN>
ngrok http 8000
```

**localtunnel** (node is installed; shows a one-time interstitial):

```powershell
.\.venv\Scripts\python.exe serve.py --http --port 8000   # in one terminal
npx localtunnel --port 8000                               # in another
```

## Notes / limits
- Keep the tunnel running only while you need it; anyone with the URL can load
  the page (they still only get their own device's mic, and must grant mic
  permission themselves).
- iOS Safari: `getUserMedia` works over the tunnel; Web Bluetooth does not
  exist on iOS. Android Chrome supports both.
- The Bluetooth audio ceiling is unchanged by hosting: still HFP 8/16 kHz, mono.
