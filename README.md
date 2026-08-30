# Bluetooth Audio Interception Research — OnePlus Buds Pro 2

Educational / **authorized, own-device** security research into the Bluetooth
audio stack. Test only devices you own, in a controlled environment. See
[`docs/ethics.md`](docs/ethics.md).

## What runs where

| Capability | Windows-native | Docker / Linux |
|---|---|---|
| BLE scan + connect + GATT (`src/ble_connect.py`) | ✅ | ✅ |
| Audio signal analysis (`src/analyzer.py`, `tools/analyze_results.py`) | ✅ | ✅ |
| Classic BT / SDP enumeration (`src/scanner.py`, `src/service_enum.py`) | ❌ | ✅ |
| SCO / HCI capture (`src/audio_capture.py`, `src/hci_monitor.py`, `btmon`) | ❌ | ✅ (real Linux host) |

**Why:** Windows Python has no `AF_BLUETOOTH` socket family, so `pybluez` and
raw SCO/HCI sockets don't exist there. BLE via `bleak` uses the Windows
WinRT stack and works natively. Encrypted modern audio links can't be
passively decoded without the link key and dedicated sniffer hardware — the
Linux tooling here observes **your own** connection's HCI/SCO traffic.

---

## Operator console (native Windows app) — recommended for multi-device

A Tkinter desktop app that embeds the hub and gives you a full control center:
live device table, listen to one or many devices at once (mixed to your chosen
output), record any of them, a **configurable storage folder**, output-device
selection, auto-record-on-connect, and persisted settings.

```bash
python hub_app.py
```

- Remote devices open the sender URL it shows (`http://<lan-ip>:<port>/`, or the
  tunnel URL) and turn on **Broadcast**.
- Select a device → **Listen** / **Record**, or **Listen all** / **Record all**.
- **Per-device voice enhancement** — select a device and adjust its chain
  (Enhance / AGC / Noise gate / High-pass, plus Gain / Sensitivity / Gate)
  from the console. It applies **server-side, per microphone**, to that
  device's playback and recordings, and is remembered per device name.
- **Settings**: pick any storage folder (Browse), output device, filename
  template (`{name} {ts} {id}`), auto-record new devices. Saved to
  `config/hub_app.json`.
- Recordings are written at each device's native rate; playback is resampled
  and mixed so devices at different rates combine cleanly.

**Local vs remote devices:**
- **Local devices (this PC)** — Bluetooth earbuds/mics paired to the machine
  running the console are captured **directly over the OS audio stack** (with
  the same voice-enhancement DSP), no browser or tunnel involved. Use the
  "Local devices" panel → *Add to devices*, then Listen/Record like any device.
- **Remote devices** — phones/other machines stream in over the hub; reach them
  beyond your LAN via the tunnel.

**Built-in tunnel:** the console can start a Cloudflare tunnel itself.
- *Start quick tunnel* → a random `trycloudflare.com` URL (no account).
- *Named tunnel* (stable URL that never changes) needs a **one-time** setup with
  your own Cloudflare account + a domain on Cloudflare:
  ```bash
  cloudflared login
  cloudflared tunnel create btmic
  cloudflared tunnel route dns btmic btmic.yourdomain.com
  ```
  Then enter name `btmic` and host `btmic.yourdomain.com` in the console and
  click *Start named*.

**Build a standalone .exe** (no Python needed to run):

```bash
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

Produces `dist\BluetoothAudioHub.exe` (bundles `webapp/` so senders still work).

---

## Multi-device live listening (hub)

Run several remote devices, see them in one local dashboard, and listen to any
of them live. Each remote browser streams its (post-DSP) mic over a WebSocket
to a hub on your PC; the dashboard lists devices and plays whichever you pick.

```bash
python hub.py            # LAN — prints the monitor URL + token
# or, reachable beyond your LAN (public HTTPS for the senders):
powershell -ExecutionPolicy Bypass -File tunnel.ps1
```

1. On each remote device: open the sender page (LAN IP or the tunnel URL),
   press **Listen**, tick **Broadcast live to hub**, give it a name.
2. On your PC: open the **monitor** link the hub prints
   (`http://localhost:8000/monitor.html?token=…`) → click a device to hear it.

**Privacy:** the monitor dashboard and live listening require the **token**
printed by the hub, so a public tunnel URL can't be used to listen to your
devices. Senders connect openly (they're the devices you set up). Audio is
Int16 PCM over WS; a ~150 ms jitter buffer smooths playback. Same HFP ceiling
(8/16 kHz, mono) as everywhere else.

## Sender page (remote device) — start broadcast on tap

The page a remote device opens (LAN IP or tunnel URL) is a **minimal sender**:
it stays idle until you press **Start broadcast**, and only then asks for the
mic and streams raw audio to the hub — no Listen/Record/enhancement controls.
All enhancement and recording happen in the operator console, per device.

- Nothing connects on page load, so simply opening the URL never touches the
  hub connection. **Stop broadcast** disconnects and stops the reconnect loop.
- The mic-permission prompt appears on the first tap of **Start broadcast**;
  that same tap also satisfies the iOS Safari user-gesture rule.
- Optional `?name=` in the URL pre-sets the device name; otherwise it
  auto-generates one you can edit on the page.
- Audio is sent **raw** (browser AGC/NS/echo-cancel disabled) so the console's
  per-device DSP is the only processing.

## Older single-page browser tool

A cross-platform web app with the same capture + enhancement, reachable from
phones/tablets on your network. See [`webapp/README.md`](webapp/README.md).

```bash
python serve.py          # HTTPS on 0.0.0.0:8443 — open https://<lan-ip>:8443 (LAN)
```

It captures the OS-exposed Bluetooth mic via `getUserMedia` (not Web Bluetooth,
which can't do audio); DSP runs in an AudioWorklet; records float32 WAV. Same
HFP ceiling (8/16 kHz, mono) applies; no exclusive-mode/mixer-bypass in browser.

**Reach it from anywhere (public HTTPS, no cert warning)** — Cloudflare Tunnel,
no account needed. See [`HOSTING.md`](HOSTING.md):

```bash
powershell -ExecutionPolicy Bypass -File tunnel.ps1   # prints a public https URL
```

---

## 0. Bluetooth Audio Monitor GUI (Windows) — listen to a paired device

A Tkinter GUI that listens to the audio stream of a **connected/paired**
Bluetooth device by capturing the microphone endpoint Windows exposes for it
(the "Hands-Free" input, e.g. `Headset (... Hands-Free (OnePlus Buds Pro 2))`).

```powershell
# one-time setup
powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1
# launch (or just double-click run_app.bat)
.\.venv\Scripts\python.exe app.py
```

In the app:
1. Pair/connect your earbuds in Windows Bluetooth settings first.
2. Pick the input endpoint — Bluetooth ones are flagged **★BT**.
3. Press **▶ Listen** — this activates the Hands-Free mic and streams it. Leave
   **Play to speakers (monitor)** on to actually hear it live.
4. Watch the level meter + live waveform.
5. **● Record** writes a WAV to `data/captures/`; **Analyze last recording**
   runs the signal analysis (voice detection, spectrum, SNR) + a PNG.

### Quality & sensitivity controls

- **Bypass mixer (exclusive/KS):** captures via WASAPI exclusive mode (or
  kernel streaming on WDM-KS endpoints) so the Windows shared mixer doesn't
  resample/process the signal. The log shows which path engaged.
- **Voice enhancement chain** (tuned for faint/distant speech):
  high-pass (cut rumble) → **Gain** → **AGC** (auto-boosts quiet speech; the
  *Sensitivity* slider sets max boost) → **Noise gate** (silences hiss between
  words; AGC won't pump the noise floor in pauses) → soft limiter (boost
  without clipping). Toggle each stage; sliders update live.
- **float32 recording** — no 16-bit quantization after enhancement.
- **Separate L/R channels** — when the selected input exposes 2+ channels
  (e.g. a mic array or stereo line-in), Record also writes each raw channel to
  its own WAV (`..._L.wav`, `..._R.wav`, …) alongside the mono mix.
  **Not available for the Bluetooth earbuds:** their mic reaches Windows as a
  single fused **mono** HFP uplink (verified — every BT endpoint reports 1
  input channel), so there is no separate left/right bud mic to split. A2DP is
  stereo but playback-only and carries no mic.
- **Negotiated-rate log** — tells you the real ceiling: 8 kHz CVSD vs 16 kHz
  mSBC/HD-Voice.

**Hard quality ceiling (physics, not a bug):** the earbuds' mic reaches Windows
over Bluetooth Hands-Free Profile, capped by the BT spec at **8 kHz (CVSD)** or
**16 kHz (mSBC / HD-Voice)**. No software can produce true hi-fi (44.1 kHz+)
from that — those frequencies are never transmitted. A2DP is hi-fi but
playback-only and carries no mic. The chain above maximizes intelligibility
within that ceiling; it does not raise the ceiling.

**How it works / limits:** this uses the standard OS audio stack. It reads only
the endpoint Windows already exposes for *your* paired device — it is not, and
on Windows cannot be, a decode of the encrypted Bluetooth RF stream of another
device. Own-device / authorized use only. Opening the Hands-Free mic switches
the earbuds to narrowband headset mode (A2DP music quality drops while
listening) — that's Bluetooth profile behavior.

---

## 1. Windows-native setup (BLE + analysis)

```powershell
# from the project root
powershell -ExecutionPolicy Bypass -File tools\setup_windows.ps1
```

Then test a real connection against a device you own (put it in
pairing/advertising mode):

```powershell
.\.venv\Scripts\python.exe src\ble_connect.py scan --timeout 12
.\.venv\Scripts\python.exe src\ble_connect.py connect <ADDRESS>
```

`connect` walks the GATT tree, reads readable characteristics, and saves the
result to `data/gatt_<addr>.json`.

Analyze a WAV capture:

```powershell
.\.venv\Scripts\python.exe tools\analyze_results.py path\to\audio.wav
```

## 2. Docker / Linux setup (Classic BT, SCO, HCI)

On a **Linux host with a Bluetooth adapter**:

```bash
docker compose build
docker compose run --rm bt-research
# inside the container:
hciconfig -a                       # confirm the adapter is visible
python main.py                     # run the full orchestrated flow
./tools/capture_audio.sh 60        # btmon HCI capture -> data/captures/*.snoop
```

> On Docker Desktop for Windows/macOS the container runs in a VM that can't
> see the host's Bluetooth radio, so the SCO/HCI parts need a real Linux host
> (or a USB BT dongle passed to a Linux VM / WSL2 via `usbipd`). Use the
> Windows-native BLE path for connect testing on this machine.

## 3. Full orchestrated run

```bash
python main.py
```

Runs discovery → enumeration → methodology → assessment and writes
`reports/final_report.md` + `reports/final_report.json`.

---

## Project layout

```
bluetooth-audio-research/
├── src/            scanner, service_enum, audio_capture, hci_monitor,
│                   analyzer, poc_framework, ble_connect
├── tools/          setup_bluetooth.sh, setup_windows.ps1,
│                   capture_audio.sh, analyze_results.py
├── config/         settings.yaml, filters.conf
├── docs/           threat_model, attack_surface, methodology, findings, ethics
├── reports/        weekly_progress, final_report (templates)
├── data/           captures/, analysis/
├── Dockerfile, docker-compose.yml
├── requirements.txt, requirements-linux.txt
└── main.py
```

## References
- Bluetooth Core Specification 5.3; HFP 1.8; A2DP 1.3.2; HSP 1.2
- NIST SP 800-121 (Guide to Bluetooth Security)
- BlueZ, PyBluez, bleak, Wireshark Bluetooth capture docs

## Disclaimer
For education and authorized testing only. Do not use for unauthorized access
or surveillance. Follow local law and responsible disclosure.
