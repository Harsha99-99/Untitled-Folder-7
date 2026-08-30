# Bluetooth Audio Monitor — Web app

Runs in any modern browser (Chrome/Edge/Firefox on Windows/macOS/Linux/Android)
and captures the audio stream of a **connected Bluetooth device** through the
browser's audio stack — the same OS-exposed mic endpoint the desktop app uses.

## How Bluetooth audio actually reaches the browser
- The mic comes in via **`getUserMedia`** (Web Audio), *not* Web Bluetooth.
- **Web Bluetooth** (`navigator.bluetooth`) only speaks **BLE GATT** — it can
  read things like battery level but **cannot capture audio**. The "BLE info"
  button is that, and is clearly labelled as info-only.
- Real-time DSP (gain → AGC → noise gate → limiter; high-pass via a biquad)
  runs in an **AudioWorklet** on the audio thread.

## Run it

```bash
# from the project root (bluetooth-audio-research/)
python serve.py                 # HTTPS on 0.0.0.0:8443 (self-signed cert)
# or, same machine only:
python serve.py --http          # http://localhost:8000
```

Then open:
- **This computer:** `https://localhost:8443` (or the `--http` localhost URL)
- **Phone / tablet / any device on your Wi-Fi:** `https://<your-LAN-ip>:8443`
  (the server prints the exact URL). Accept the one-time self-signed warning
  → *Advanced → proceed*. After that it's a secure context and mic works.

In the page: pick the input (Bluetooth ones flagged ★BT) → **Listen** →
enhancement sliders live-update → **Record** downloads a float32 WAV.

## Why HTTPS?
`getUserMedia` (and Web Bluetooth) require a **secure context**. `localhost`
counts as secure, but any *other* device must reach the page over HTTPS —
hence the self-signed server.

## Cross-device reality (important)
- The page captures the mic of the device the **browser is running on**. Open
  it on your phone → it captures the phone's paired Bluetooth mic. It cannot
  reach across to another machine's Bluetooth.
- **No exclusive mode:** browsers use shared-mode audio only, so the desktop
  app's "bypass mixer" win isn't available here. We *do* disable the browser's
  built-in AGC/noise-suppression/echo-cancellation so our DSP is the only
  processing (rawer, more sensitive).
- **Same ceiling:** Bluetooth HFP is still 8/16 kHz and **mono**, so there is
  no separate L/R bud mic to record. The page logs the negotiated rate.
- **iOS:** Safari supports `getUserMedia` (limited device picking) but **no**
  Web Bluetooth. Android Chrome supports both.

Own-device / authorized use only.
