#!/usr/bin/env python3
# hub_app.py — native operator console (Tkinter) for multi-device live listening.
#
# Embeds the HubServer, shows every remote device that is broadcasting, and lets
# you listen to one or many at once (mixed to your chosen output device) and
# record any of them to a storage folder you pick. Settings persist.
#
#   python hub_app.py
#
# Remote devices still open the sender page served by this same hub
# (http://<lan-ip>:<port>/  or the tunnel URL).

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
import webbrowser
from datetime import datetime

import numpy as np
import sounddevice as sd
from scipy.signal import butter, lfilter

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import hub  # HubServer, lan_ip, BASE, DEFAULT_CAPTURES

CONFIG_PATH = os.path.join(hub.BASE, "config", "hub_app.json")


def db_to_lin(db):
    return float(10.0 ** (db / 20.0))


# --------------------------------------------------------------------------
# voice DSP for LOCAL capture (high-pass -> gain -> AGC -> gate -> limiter)
# --------------------------------------------------------------------------
class VoiceDSP:
    def __init__(self, samplerate):
        self.sr = samplerate
        self.enabled = True
        self.gain = db_to_lin(12.0)
        self.agc = True
        self.agc_target = db_to_lin(-18.0)
        self.agc_max_gain = db_to_lin(34.0)
        self._agc_gain = 1.0
        self.gate = True
        self.gate_thresh = db_to_lin(-58.0)
        self._gate_env = 0.0
        self.highpass = True
        self._build_hp(110.0)

    def _build_hp(self, fc):
        ny = self.sr / 2.0
        fc = min(fc, ny * 0.9)
        self._b, self._a = butter(2, fc / ny, btype="high")
        self._zi = np.zeros(max(len(self._a), len(self._b)) - 1, dtype=np.float64)

    def process(self, x):
        if not self.enabled:
            return x.astype(np.float32, copy=False)
        y = x.astype(np.float64)
        if self.highpass:
            y, self._zi = lfilter(self._b, self._a, y, zi=self._zi)
        y = y * self.gain
        raw_rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) + 1e-9
        if self.agc:
            cur = float(np.sqrt(np.mean(y ** 2))) + 1e-9
            desired = min(self.agc_target / cur, self.agc_max_gain)
            below = self.gate and (raw_rms <= self.gate_thresh)
            if below and desired > self._agc_gain:
                desired = self._agc_gain
            coeff = 0.25 if desired < self._agc_gain else 0.12
            self._agc_gain = (1 - coeff) * self._agc_gain + coeff * desired
            y = y * self._agc_gain
        if self.gate:
            target = 1.0 if raw_rms > self.gate_thresh else 0.0
            coeff = 0.30 if target >= self._gate_env else 0.06
            self._gate_env = (1 - coeff) * self._gate_env + coeff * target
            y = y * self._gate_env
        a = np.abs(y)
        knee = a > 0.7
        y[knee] = np.sign(y[knee]) * (0.7 + 0.3 * np.tanh((a[knee] - 0.7) / 0.3))
        return y.astype(np.float32)


# --------------------------------------------------------------------------
# streaming linear resampler (device rate -> output rate, click-free)
# --------------------------------------------------------------------------
class LinearResampler:
    def __init__(self, in_sr, out_sr):
        self.in_sr = int(in_sr)
        self.out_sr = int(out_sr)
        self.step = self.in_sr / self.out_sr        # input samples per output sample
        self.next_pos = 0.0
        self.tail = np.zeros(0, dtype=np.float32)

    def process(self, x):
        if self.in_sr == self.out_sr:
            return x.astype(np.float32, copy=False)
        buf = np.concatenate((self.tail, x)) if self.tail.size else x
        if buf.size < 2:
            self.tail = buf
            return np.zeros(0, dtype=np.float32)
        last = buf.size - 1
        positions = []
        p = self.next_pos
        while p <= last - 1e-9:
            positions.append(p)
            p += self.step
        if positions:
            pos = np.asarray(positions, dtype=np.float64)
            idx = np.floor(pos).astype(np.int64)
            frac = (pos - idx).astype(np.float32)
            out = (buf[idx] * (1.0 - frac) + buf[idx + 1] * frac).astype(np.float32)
        else:
            out = np.zeros(0, dtype=np.float32)
        # carry last sample for continuity; shift position frame
        self.tail = buf[-1:].copy()
        self.next_pos = max(0.0, p - (buf.size - 1))
        return out


# --------------------------------------------------------------------------
# per-device stream: buffers playback (at out_sr) and records (at native sr)
# --------------------------------------------------------------------------
class DeviceStream:
    def __init__(self, sid, name, dev_sr, out_sr):
        self.sid = sid
        self.name = name
        self.dev_sr = int(dev_sr)
        self.out_sr = int(out_sr)
        self.resampler = LinearResampler(dev_sr, out_sr)
        self.play = np.zeros(0, dtype=np.float32)
        self.lock = threading.Lock()
        self.vol = 1.0
        self.playing = False
        self.max_samples = int(out_sr * 1.0)  # cap ~1 s of latency
        # Jitter buffer. Network audio arrives in ~50 ms bursts with variable
        # delay, but the output callback asks for a few ms at a time, so playing
        # the instant the first sample lands guarantees an underrun on the next
        # hiccup - heard as constant dropouts. Hold ~180 ms before starting, and
        # rebuild that cushion after any underrun instead of stuttering. Sized
        # to comfortably cover the sender's 250 ms batch interval - a cushion
        # smaller than the inter-arrival gap underruns on every batch.
        self.target_buf = int(out_sr * 0.40)
        self.armed = False
        self.underruns = 0
        self.level_rms = 0.0
        self.rec = None
        self.rec_path = None
        self.rec_lock = threading.Lock()
        # per-device voice enhancement (server-side, operator-controlled)
        self.dsp = VoiceDSP(self.dev_sr)

    def feed(self, i16_bytes):
        i16 = np.frombuffer(i16_bytes, dtype="<i2")
        if i16.size == 0:
            return
        f = i16.astype(np.float32) / 32768.0
        y = self.dsp.process(f)                     # enhancement applied here
        with self.rec_lock:
            if self.rec is not None:
                yi = np.clip(y, -1.0, 1.0)
                self.rec.writeframes((yi * 32767.0).astype("<i2").tobytes())
        self.level_rms = float(np.sqrt(np.mean(y * y))) if y.size else 0.0
        if self.playing:
            rs = self.resampler.process(y)
            if rs.size:
                with self.lock:
                    self.play = np.concatenate((self.play, rs))
                    if self.play.size > self.max_samples:
                        self.play = self.play[-self.max_samples:]

    def read(self, n):
        with self.lock:
            if not self.armed:
                # still filling the cushion - stay silent rather than emit a
                # fragment we cannot follow up on
                if self.play.size < self.target_buf:
                    return np.zeros(n, dtype=np.float32)
                self.armed = True
            if self.play.size >= n:
                out = self.play[:n]; self.play = self.play[n:]
            else:
                out = np.zeros(n, dtype=np.float32)
                out[: self.play.size] = self.play
                self.play = np.zeros(0, dtype=np.float32)
                self.armed = False       # refill before speaking again
                self.underruns += 1
        return out * self.vol

    def start_record(self, path):
        with self.rec_lock:
            if self.rec is not None:
                return
            w = wave.open(path, "wb")
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.dev_sr)
            self.rec = w; self.rec_path = path

    def stop_record(self):
        with self.rec_lock:
            if self.rec is None:
                return None
            try:
                self.rec.close()
            except Exception:
                pass
            p = self.rec_path
            self.rec = None; self.rec_path = None
            return p


# --------------------------------------------------------------------------
# audio engine: single output stream mixing all playing DeviceStreams
# --------------------------------------------------------------------------
class AudioEngine:
    def __init__(self, out_sr=48000, device=None):
        self.out_sr = int(out_sr)
        self.device = device
        self.master = 1.0
        self.streams = {}
        self.lock = threading.Lock()
        self.stream = None
        self.clip = 0
        # health telemetry: PortAudio aborts an output stream if the callback
        # raises, which silences the console for good while the browser monitor
        # (a completely separate path) keeps working. Track liveness so the UI
        # can notice and restart instead of going quietly dead.
        self.last_cb = 0.0
        self.cb_errors = 0
        self.last_error = ""

    def start(self):
        self.stop()
        self.stream = sd.OutputStream(
            samplerate=self.out_sr, channels=2, dtype="float32",
            device=self.device, blocksize=0, latency="low", callback=self._cb,
        )
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _cb(self, outdata, frames, t, status):
        self.last_cb = time.monotonic()
        mix = np.zeros(frames, dtype=np.float32)
        with self.lock:
            streams = [s for s in self.streams.values() if s.playing]
        for s in streams:
            # one misbehaving device must never take the whole output down
            try:
                chunk = s.read(frames)
                if chunk.shape[0] == frames:
                    mix += chunk
            except Exception as e:                      # noqa: BLE001 - realtime
                self.cb_errors += 1
                self.last_error = f"{getattr(s, 'name', '?')}: {e}"
        mix *= self.master
        peak = np.max(np.abs(mix)) if mix.size else 0.0
        if peak > 1.0:
            self.clip += 1
            np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:, 0] = mix
        if outdata.shape[1] > 1:
            outdata[:, 1] = mix

    def playing_count(self):
        with self.lock:
            return sum(1 for s in self.streams.values() if s.playing)

    def healthy(self):
        """False when the output callback has stopped firing - a dead stream."""
        if self.stream is None:
            return False
        return (time.monotonic() - self.last_cb) < 1.0

    def add(self, s):
        with self.lock:
            self.streams[s.sid] = s

    def remove(self, sid):
        with self.lock:
            self.streams.pop(sid, None)


# --------------------------------------------------------------------------
# feeder thread: drains a hub tap queue into a DeviceStream
# --------------------------------------------------------------------------
class Feeder(threading.Thread):
    def __init__(self, q, ds):
        super().__init__(daemon=True)
        self.q = q; self.ds = ds; self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                data = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.ds.feed(data)
            except Exception:
                pass

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------
# local source: capture a Bluetooth/mic input on THIS PC straight into the mix
# --------------------------------------------------------------------------
BT_HINTS = ("hands-free", "headset", "bluetooth", "buds", "airpods", "hfp", "bthhfenum")

# Windows exposes a Bluetooth Hands-Free (HFP) capture endpoint through
# bthhfenum.sys under a raw resource string, e.g.
#   Headset (@System32\drivers\bthhfenum.sys,#2;%1 Hands-Free%0<LF>;(boAt Stone 650))
# which is unreadable in a combo box and even contains a newline, so operators
# can't tell which entry is their speaker. Pull the friendly name back out.
_HFP_DEV_RE = re.compile(r";\(\s*([^()]+?)\s*\)\s*\)?\s*$")
_HFP_PROFILE_RE = re.compile(r"%1\s*([^%]+?)\s*%0")


def pretty_input_name(raw):
    """Map the raw bthhfenum resource string above to a readable label, e.g.
    'boAt Stone 650 (Hands-Free)'. Non-HFP names pass through unchanged."""
    flat = " ".join(str(raw).split())
    m = _HFP_DEV_RE.search(flat)
    if not m:
        return flat
    prof = _HFP_PROFILE_RE.search(flat)
    return f"{m.group(1)} ({prof.group(1) if prof else 'Hands-Free'})"


def _can_open_input(idx, sr):
    """Windows keeps an HFP capture endpoint registered for every *paired*
    device, connected or not, so query_devices() alone can't tell you which
    ones are usable. Opening the stream can: a stale endpoint fails instantly
    (PaErrorCode -9999), a live one costs a few hundred ms."""
    try:
        sd.InputStream(device=idx, channels=1, samplerate=sr, blocksize=256).close()
        return True
    except Exception:
        return False


def rescan_portaudio():
    """PortAudio snapshots the device table when it initialises, so a headset
    that connects (or drops out of Hands-Free) while the console is running
    leaves a stale list behind. Worse, indices renumber every time a Bluetooth
    device comes or goes, so a remembered index can quietly address a totally
    different endpoint. Re-initialise before every enumeration."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def resolve_input_index(idx, raw, api):
    """Return the current index of the endpoint that was enumerated as
    (raw, api), or None if it has gone away. Guards against the renumbering
    described in rescan_portaudio()."""
    try:
        d = sd.query_devices(idx)
        if d["name"] == raw and sd.query_hostapis(d["hostapi"])["name"] == api                 and d["max_input_channels"] >= 1:
            return idx
    except Exception:
        pass
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] < 1:
                continue
            if d["name"] == raw and sd.query_hostapis(d["hostapi"])["name"] == api:
                return i
    except Exception:
        pass
    return None


def local_input_devices(probe=True, rescan=True):
    """[(key, label, is_bt, index, sr, live, raw, api)] for input endpoints.

    Bluetooth entries are probed for liveness (cheap: dead ones fail
    immediately) and sorted first, so a connected headset mic is the default
    pick and paired-but-offline leftovers sink to the bottom. `raw`/`api` are
    kept so the index can be re-resolved at open time."""
    if rescan:
        rescan_portaudio()
    out = []
    try:
        for idx, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] < 1:
                continue
            api = sd.query_hostapis(d["hostapi"])["name"]
            raw = d["name"]
            is_bt = any(h in raw.lower() for h in BT_HINTS)
            sr = int(round(d.get("default_samplerate") or 48000)) or 48000
            live = _can_open_input(idx, sr) if (is_bt and probe) else True
            suffix = "" if live else "  — paired, not connected"
            label = f"[{idx}] {pretty_input_name(raw)} ({api}){suffix}"
            # key must survive index renumbering AND be filename-safe, since
            # recordings interpolate it into their name
            slug = re.sub(r"[^A-Za-z0-9]+", "-",
                          f"{pretty_input_name(raw)} {api}").strip("-").lower()
            out.append((f"local:{slug}", label, is_bt, idx, sr, live, raw, api))
    except Exception:
        pass
    out.sort(key=lambda r: (not r[5], not r[2], r[3]))
    return out


class LocalSource:
    """Captures a local input device, runs VoiceDSP, feeds a DeviceStream so it
    mixes/records exactly like a remote device — but with no network hop."""

    def __init__(self, key, name, dev_index, dev_sr, out_sr):
        self.key = key
        self.name = name
        self.dev_index = dev_index
        self.dev_sr = int(dev_sr)
        self.ds = DeviceStream(key, name, self.dev_sr, out_sr)  # DSP lives on ds
        self.stream = None

    def start(self):
        if self.stream is not None:
            return
        self.stream = sd.InputStream(
            samplerate=self.dev_sr, channels=1, dtype="float32",
            device=self.dev_index, blocksize=1024, latency="low", callback=self._cb,
        )
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        # send RAW mono to the DeviceStream; enhancement is applied there
        mono = indata[:, 0] if indata.ndim > 1 else indata.reshape(-1)
        i16 = np.clip(mono, -1.0, 1.0)
        i16 = (i16 * 32767.0).astype("<i2")
        self.ds.feed(i16.tobytes())

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "port": 8000,
    "storage_dir": hub.DEFAULT_CAPTURES,
    "output_device": None,     # sounddevice index or None (system default)
    "out_sr": 48000,
    "auto_record": False,
    "filename_template": "{name}_{ts}.wav",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def output_devices():
    out = [("System default", None)]
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                api = sd.query_hostapis(d["hostapi"])["name"]
                out.append((f"[{i}] {d['name']} ({api})", i))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bluetooth Audio Hub — Operator Console")
        self.geometry("1040x760")
        self.minsize(900, 680)

        self.cfg = load_config()
        self.hub = None
        self.engine = AudioEngine(self.cfg["out_sr"], self.cfg["output_device"])
        self.active = {}   # key -> session dict (kind remote|local)
        self.row_iid = {}  # key -> tree iid
        self._out_devs = output_devices()
        self._local_devs = local_input_devices()
        self.tunnel_proc = None
        self.tunnel_url = None
        self.dsp_settings = dict(self.cfg.get("dsp_settings", {}))  # name -> params
        self._loading_dsp = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_hub()
        self.after(400, self._tick)

    # ---- layout ----
    def _build_ui(self):
        pad = {"padx": 8, "pady": 5}

        # server
        srv = ttk.LabelFrame(self, text="Hub server")
        srv.pack(fill="x", **pad)
        r = ttk.Frame(srv); r.pack(fill="x", padx=8, pady=6)
        ttk.Label(r, text="Port").pack(side="left")
        self.port_var = tk.IntVar(value=self.cfg["port"])
        ttk.Entry(r, textvariable=self.port_var, width=7).pack(side="left", padx=6)
        self.srv_btn = ttk.Button(r, text="Restart", command=self.restart_hub)
        self.srv_btn.pack(side="left", padx=6)
        self.srv_status = ttk.Label(r, text="starting…", foreground="#f6ad55")
        self.srv_status.pack(side="left", padx=10)
        ttk.Button(r, text="Open monitor (browser)", command=self.open_monitor).pack(side="right")

        r2 = ttk.Frame(srv); r2.pack(fill="x", padx=8, pady=(0, 4))
        self.sender_lbl = ttk.Label(r2, text="Sender URL: —", cursor="hand2")
        self.sender_lbl.pack(side="left")
        self.copy_btn = ttk.Button(r2, text="Copy sender URL", command=self.copy_sender)
        self.copy_btn.pack(side="right")

        # tunnel row — remote devices reach the sender page through this
        r3 = ttk.Frame(srv); r3.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(r3, text="Tunnel:").pack(side="left")
        self.tunnel_btn = ttk.Button(r3, text="Start quick tunnel", command=self.toggle_quick_tunnel)
        self.tunnel_btn.pack(side="left", padx=6)
        ttk.Label(r3, text="or named:").pack(side="left", padx=(10, 2))
        self.named_var = tk.StringVar(value=self.cfg.get("named_tunnel", ""))
        ttk.Entry(r3, textvariable=self.named_var, width=16).pack(side="left")
        ttk.Label(r3, text="host:").pack(side="left", padx=(6, 2))
        self.host_var = tk.StringVar(value=self.cfg.get("tunnel_host", ""))
        ttk.Entry(r3, textvariable=self.host_var, width=22).pack(side="left")
        self.named_btn = ttk.Button(r3, text="Start named", command=self.start_named_tunnel)
        self.named_btn.pack(side="left", padx=6)
        self.tunnel_lbl = ttk.Label(r3, text="(off)", foreground="#8b98a5")
        self.tunnel_lbl.pack(side="left", padx=8)
        ttk.Button(r3, text="Copy tunnel URL", command=self.copy_tunnel).pack(side="right")

        # devices
        dev = ttk.LabelFrame(self, text="Live devices")
        dev.pack(fill="both", expand=True, **pad)
        cols = ("name", "id", "sr", "level", "listen", "record")
        self.tree = ttk.Treeview(dev, columns=cols, show="headings", height=9, selectmode="browse")
        for c, w, t in [("name", 240, "Device"), ("id", 50, "ID"), ("sr", 90, "Rate"),
                        ("level", 130, "Level"), ("listen", 90, "Listening"), ("record", 90, "Recording")]:
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sync_selection())

        # per-device controls
        ctl = ttk.Frame(dev); ctl.pack(fill="x", padx=8, pady=(0, 8))
        self.listen_btn = ttk.Button(ctl, text="▶ Listen", command=self.toggle_listen, state="disabled")
        self.listen_btn.pack(side="left")
        self.record_btn = ttk.Button(ctl, text="● Record", command=self.toggle_record, state="disabled")
        self.record_btn.pack(side="left", padx=6)
        ttk.Label(ctl, text="Vol").pack(side="left", padx=(14, 2))
        self.vol_var = tk.DoubleVar(value=100)
        ttk.Scale(ctl, from_=0, to=150, variable=self.vol_var, length=150,
                  command=lambda e: self._apply_vol()).pack(side="left")
        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(ctl, text="Listen all", command=self.listen_all).pack(side="left")
        ttk.Button(ctl, text="Record all", command=self.record_all).pack(side="left", padx=6)
        ttk.Button(ctl, text="Stop all", command=self.stop_all).pack(side="left")

        # local devices (this PC — paired Bluetooth captured directly, no tunnel)
        loc = ttk.LabelFrame(self, text="Local devices (this PC — direct Bluetooth / mic)")
        loc.pack(fill="x", **pad)
        lr = ttk.Frame(loc); lr.pack(fill="x", padx=8, pady=6)
        self.local_var = tk.StringVar()
        self.local_combo = ttk.Combobox(lr, textvariable=self.local_var, width=56, state="readonly")
        self.local_combo.pack(side="left", padx=(0, 6))
        ttk.Button(lr, text="Refresh", command=self.refresh_local).pack(side="left")
        ttk.Button(lr, text="Add to devices", command=self.add_local).pack(side="left", padx=6)
        ttk.Label(lr, text="(then Listen/Record it above)", foreground="#8b98a5").pack(side="left")
        self._fill_local_combo()

        # per-device voice enhancement (applies to the SELECTED device)
        enh = ttk.LabelFrame(self, text="Voice enhancement — selected device (server-side, per device)")
        enh.pack(fill="x", **pad)
        e1 = ttk.Frame(enh); e1.pack(fill="x", padx=8, pady=4)
        self.e_enh = tk.BooleanVar(value=True)
        self.e_agc = tk.BooleanVar(value=True)
        self.e_gate = tk.BooleanVar(value=True)
        self.e_hp = tk.BooleanVar(value=True)
        for txt, var in [("Enhance", self.e_enh), ("AGC", self.e_agc),
                         ("Noise gate", self.e_gate), ("High-pass", self.e_hp)]:
            ttk.Checkbutton(e1, text=txt, variable=var,
                            command=self._on_dsp_change).pack(side="left", padx=6)
        self.enh_which = ttk.Label(e1, text="(no device selected)", foreground="#8b98a5")
        self.enh_which.pack(side="right")

        e2 = ttk.Frame(enh); e2.pack(fill="x", padx=8, pady=4)
        ttk.Label(e2, text="Gain", width=6).pack(side="left")
        self.e_gain = tk.DoubleVar(value=12)
        ttk.Scale(e2, from_=0, to=40, variable=self.e_gain, length=150,
                  command=lambda x: self._on_dsp_change()).pack(side="left")
        self.e_gain_lbl = ttk.Label(e2, text="+12 dB", width=8); self.e_gain_lbl.pack(side="left")
        ttk.Label(e2, text="Sensitivity", width=10).pack(side="left", padx=(12, 0))
        self.e_sens = tk.DoubleVar(value=34)
        ttk.Scale(e2, from_=0, to=48, variable=self.e_sens, length=150,
                  command=lambda x: self._on_dsp_change()).pack(side="left")
        self.e_sens_lbl = ttk.Label(e2, text="+34 dB", width=8); self.e_sens_lbl.pack(side="left")
        ttk.Label(e2, text="Gate", width=6).pack(side="left", padx=(12, 0))
        self.e_gatedb = tk.DoubleVar(value=-58)
        ttk.Scale(e2, from_=-80, to=-30, variable=self.e_gatedb, length=130,
                  command=lambda x: self._on_dsp_change()).pack(side="left")
        self.e_gate_lbl = ttk.Label(e2, text="-58 dBFS", width=10); self.e_gate_lbl.pack(side="left")

        # settings
        st = ttk.LabelFrame(self, text="Settings")
        st.pack(fill="x", **pad)
        s1 = ttk.Frame(st); s1.pack(fill="x", padx=8, pady=6)
        ttk.Label(s1, text="Storage folder", width=14).pack(side="left")
        self.store_var = tk.StringVar(value=self.cfg["storage_dir"])
        ttk.Entry(s1, textvariable=self.store_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(s1, text="Browse…", command=self.pick_folder).pack(side="left")
        ttk.Button(s1, text="Open", command=self.open_folder).pack(side="left", padx=6)

        s2 = ttk.Frame(st); s2.pack(fill="x", padx=8, pady=6)
        ttk.Label(s2, text="Output device", width=14).pack(side="left")
        self.out_var = tk.StringVar()
        self.out_combo = ttk.Combobox(s2, textvariable=self.out_var, width=52, state="readonly",
                                      values=[d[0] for d in self._out_devs])
        self.out_combo.pack(side="left", padx=6)
        self.out_combo.current(self._out_index_default())
        self.out_combo.bind("<<ComboboxSelected>>", lambda e: self.change_output())
        self.auto_var = tk.BooleanVar(value=self.cfg["auto_record"])
        ttk.Checkbutton(s2, text="Auto-record new devices", variable=self.auto_var,
                        command=self.save_settings).pack(side="left", padx=14)

        s3 = ttk.Frame(st); s3.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(s3, text="Filename", width=14).pack(side="left")
        self.fname_var = tk.StringVar(value=self.cfg["filename_template"])
        ttk.Entry(s3, textvariable=self.fname_var, width=30).pack(side="left", padx=6)
        ttk.Label(s3, text="{name} {ts} {id}", foreground="#8b98a5").pack(side="left")
        ttk.Button(s3, text="Save settings", command=self.save_settings).pack(side="right")

        # log
        self.log_txt = tk.Text(self, height=7, wrap="word")
        self.log_txt.pack(fill="both", expand=False, **pad)
        self.log_txt.configure(state="disabled")

    def _out_index_default(self):
        want = self.cfg["output_device"]
        for i, (_, idx) in enumerate(self._out_devs):
            if idx == want:
                return i
        return 0

    # ---- hub lifecycle ----
    def start_hub(self):
        try:
            self.hub = hub.HubServer(port=int(self.port_var.get()), captures_dir=self.store_var.get())
            self.hub.start()
            self.engine.start()
            ip = hub.lan_ip()
            self.sender_url = f"http://{ip}:{self.hub.port}/"
            self.monitor_url = f"http://localhost:{self.hub.port}/monitor.html?token={self.hub.token}"
            self.sender_lbl.config(text=f"Sender URL: {self.sender_url}   |   token: {self.hub.token}")
            self.srv_status.config(text="running", foreground="#4fd1c5")
            self.log(f"[+] Hub running on port {self.hub.port}.")
            self.log(f"[i] Remote devices open: {self.sender_url}")
            self.log(f"[i] (Or the tunnel URL — run tunnel.ps1 separately.)")
        except Exception as e:
            self.srv_status.config(text="error", foreground="#e53e3e")
            self.log(f"[-] Hub failed: {e}")
            messagebox.showerror("Hub error", str(e))

    def restart_hub(self):
        self.stop_all()
        self.engine.stop()
        if self.hub:
            self.hub.stop()
        self.cfg["port"] = int(self.port_var.get())
        save_config(self.cfg)
        self.start_hub()

    # ---- device activation ----
    def _ensure_active(self, sid, name, dev_sr):
        """Remote device: create tap+feeder on demand."""
        if sid in self.active:
            return self.active[sid]
        q = self.hub.register_tap(sid)
        ds = DeviceStream(sid, name, dev_sr, self.engine.out_sr)
        self._apply_settings_to_ds(ds, self.dsp_settings.setdefault(name, self._default_dsp()))
        feeder = Feeder(q, ds); feeder.start()
        self.engine.add(ds)
        rec = {"kind": "remote", "q": q, "ds": ds, "feeder": feeder, "listen": False, "record": False}
        self.active[sid] = rec
        return rec

    def _deactivate_if_idle(self, sid):
        rec = self.active.get(sid)
        if not rec or rec["listen"] or rec["record"] or rec.get("kind") != "remote":
            return
        rec["feeder"].stop()
        self.hub.unregister_tap(sid, rec["q"])
        self.engine.remove(sid)
        self.active.pop(sid, None)

    def _selected_sid(self):
        sel = self.tree.selection()
        if not sel:
            return None
        for sid, iid in self.row_iid.items():
            if iid == sel[0]:
                return sid
        return None

    def _dev_info(self, sid):
        rec = self.active.get(sid)
        if rec and rec.get("kind") == "local":
            s = rec["src"]
            return {"id": sid, "name": s.name, "sr": s.dev_sr}
        for d in self.hub.snapshot_devices():
            if d["id"] == sid:
                return d
        return None

    def _get_or_create(self, sid):
        """Return the session for sid; create it for remote devices on demand.
        Local devices must already exist (added via 'Add to devices')."""
        rec = self.active.get(sid)
        if rec is not None:
            return rec
        d = self._dev_info(sid)
        if not d:
            return None
        return self._ensure_active(sid, d["name"], d["sr"])

    def toggle_listen(self):
        sid = self._selected_sid()
        if sid is None:
            return
        rec = self._get_or_create(sid)
        if not rec:
            return
        rec["listen"] = not rec["listen"]
        rec["ds"].playing = rec["listen"]
        if not rec["listen"]:
            self._deactivate_if_idle(sid)
        self.log(f"[i] {'Listening to' if rec.get('listen') else 'Stopped'} {rec['ds'].name}.")
        self._refresh_rows(); self._sync_selection()

    def toggle_record(self):
        sid = self._selected_sid()
        if sid is None:
            return
        rec = self._get_or_create(sid)
        if not rec:
            return
        if rec["record"]:
            path = rec["ds"].stop_record(); rec["record"] = False
            self.log(f"[+] Saved: {path}")
            self._deactivate_if_idle(sid)
        else:
            path = self._make_path(self._dev_info(sid))
            rec["ds"].start_record(path); rec["record"] = True
            self.log(f"[*] Recording {rec['ds'].name} -> {path}")
        self._refresh_rows(); self._sync_selection()

    def listen_all(self):
        for d in self.hub.snapshot_devices():
            rec = self._ensure_active(d["id"], d["name"], d["sr"])
            rec["listen"] = True; rec["ds"].playing = True
        for rec in self.active.values():          # include local devices
            if rec.get("kind") == "local":
                rec["listen"] = True; rec["ds"].playing = True
        self.log("[i] Listening to all devices (remote + local).")
        self._refresh_rows()

    def record_all(self):
        for d in self.hub.snapshot_devices():
            rec = self._ensure_active(d["id"], d["name"], d["sr"])
            if not rec["record"]:
                path = self._make_path(d); rec["ds"].start_record(path); rec["record"] = True
                self.log(f"[*] Recording {d['name']} -> {path}")
        for sid, rec in list(self.active.items()):
            if rec.get("kind") == "local" and not rec["record"]:
                path = self._make_path(self._dev_info(sid))
                rec["ds"].start_record(path); rec["record"] = True
                self.log(f"[*] Recording {rec['ds'].name} -> {path}")
        self._refresh_rows()

    def stop_all(self):
        for sid in list(self.active.keys()):
            rec = self.active[sid]
            if rec["record"]:
                self.log(f"[+] Saved: {rec['ds'].stop_record()}")
            rec["listen"] = False; rec["record"] = False; rec["ds"].playing = False
            if rec.get("kind") == "local":
                rec["src"].stop()
            else:
                rec["feeder"].stop()
                if self.hub:
                    self.hub.unregister_tap(sid, rec["q"])
            self.engine.remove(sid)
        self.active.clear()
        self._refresh_rows()

    def _make_path(self, d):
        os.makedirs(self.store_var.get(), exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^A-Za-z0-9 _-]", "", str(d["name"])).strip().replace(" ", "_") or "device"
        idv = str(d.get("id", "")).replace("local:", "L")
        try:
            fn = self.fname_var.get().format(name=safe_name, ts=ts, id=idv)
        except Exception:
            fn = f"{safe_name}_{ts}.wav"
        fn = re.sub(r'[<>:"/\\|?*]', "_", os.path.basename(fn))
        if not fn.lower().endswith(".wav"):
            fn += ".wav"
        return os.path.join(self.store_var.get(), fn)

    # ---- local devices (direct Bluetooth on this PC) ----
    def _fill_local_combo(self):
        labels, bt_first, first_live = [], None, None
        for i, (key, label, is_bt, idx, sr, live, raw, api) in enumerate(self._local_devs):
            labels.append(label + ("  ★BT" if is_bt else ""))
            if live and first_live is None:
                first_live = i
            if is_bt and live and bt_first is None:
                bt_first = i
        self.local_combo["values"] = labels
        if labels:
            pick = bt_first if bt_first is not None else (first_live or 0)
            self.local_combo.current(pick)

    def refresh_local(self):
        self._local_devs = local_input_devices()
        self._fill_local_combo()
        live = [d for d in self._local_devs if d[5]]
        bt_live = [d for d in live if d[2]]
        stale = len(self._local_devs) - len(live)
        self.log(f"[i] {len(live)} usable local input endpoint(s)"
                 f" — {len(bt_live)} Bluetooth."
                 + (f" {stale} paired but not connected (listed last)." if stale else ""))
        if not bt_live:
            self.log("[i] No Bluetooth mic available. A speaker only shows up here if it "
                     "has a microphone and is connected on the Hands-Free profile — "
                     "output-only speakers (plain A2DP, no mic) cannot be a source.")

    def add_local(self):
        sel = self.local_combo.current()
        if sel < 0 or sel >= len(self._local_devs):
            return
        key, label, is_bt, idx, sr, live, raw, api = self._local_devs[sel]
        if key in self.active:
            self.log("[i] That local device is already added."); return
        name = label.split("] ", 1)[-1].split(" (")[0]
        if not live:
            msg = (f"'{name}' is paired but not connected, so Windows can't open its "
                   "microphone. Connect the device, set it to Hands-Free / Headset in "
                   "Sound settings, then press Refresh.")
            self.log(f"[-] {msg}")
            messagebox.showwarning("Device not connected", msg); return
        # Bluetooth connect/disconnect renumbers PortAudio indices, so the index
        # captured at Refresh time may now address a different endpoint entirely.
        real = resolve_input_index(idx, raw, api)
        if real is None:
            msg = (f"'{name}' is no longer available - Windows dropped the endpoint "
                   "(a headset usually leaves Hands-Free when nothing is holding the "
                   "mic). Reconnect it, then press Refresh.")
            self.log(f"[-] {msg}")
            messagebox.showwarning("Device gone", msg); return
        if real != idx:
            self.log(f"[i] '{name}' moved from index {idx} to {real} since the last "
                     "refresh - using the current one.")
            idx = real
        try:
            src = LocalSource(key, name, idx, sr, self.engine.out_sr)
            src.start()
        except Exception as e:
            self.log(f"[-] Local capture failed: {e}")
            messagebox.showerror("Local capture", str(e)); return
        self._apply_settings_to_ds(src.ds, self.dsp_settings.setdefault(name, self._default_dsp()))
        self.engine.add(src.ds)
        self.active[key] = {"kind": "local", "src": src, "ds": src.ds, "listen": False, "record": False}
        self.log(f"[+] Added local device '{name}' @ {sr} Hz — select it above to Listen/Record.")
        self._refresh_rows()

    # ---- tunnel (Cloudflare) ----
    def _cloudflared_path(self):
        p = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
        return p if os.path.isfile(p) else shutil.which("cloudflared")

    def toggle_quick_tunnel(self):
        if self.tunnel_proc:
            self.stop_tunnel(); return
        if not self.hub:
            return
        self._start_tunnel(["tunnel", "--url", f"http://localhost:{self.hub.port}", "--no-autoupdate"], named=False)

    def start_named_tunnel(self):
        name = self.named_var.get().strip()
        host = self.host_var.get().strip()
        if not name:
            messagebox.showinfo("Named tunnel",
                                "Enter your cloudflared tunnel NAME first.\n\n"
                                "One-time setup (needs a Cloudflare account + a domain on Cloudflare):\n"
                                "  cloudflared login\n"
                                "  cloudflared tunnel create btmic\n"
                                "  cloudflared tunnel route dns btmic btmic.yourdomain.com\n\n"
                                "Then put 'btmic' as name and 'btmic.yourdomain.com' as host.")
            return
        self.cfg["named_tunnel"] = name; self.cfg["tunnel_host"] = host; save_config(self.cfg)
        if self.tunnel_proc:
            self.stop_tunnel()
        self._start_tunnel(["tunnel", "run", "--url", f"http://localhost:{self.hub.port}", name],
                           named=True, host=host)

    def _start_tunnel(self, args, named, host=None):
        cf = self._cloudflared_path()
        if not cf:
            messagebox.showerror("cloudflared",
                                 "cloudflared not found.\nInstall: winget install --id Cloudflare.cloudflared")
            return
        try:
            self.tunnel_proc = subprocess.Popen(
                [cf] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            messagebox.showerror("cloudflared", str(e)); return
        self.tunnel_btn.config(text="Stop tunnel")
        self.tunnel_lbl.config(text="starting…", foreground="#f6ad55")
        if named and host:
            self.tunnel_url = f"https://{host}/"
            self.tunnel_lbl.config(text=self.tunnel_url, foreground="#4fd1c5")
            self.log(f"[+] Named tunnel → {self.tunnel_url} (stable). Give this to remote devices.")
        else:
            self.log("[*] Quick tunnel starting… URL will appear here.")
        threading.Thread(target=self._read_tunnel, args=(named,), daemon=True).start()

    def _read_tunnel(self, named):
        proc = self.tunnel_proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not named:
                url = m.group(0) + "/"
                self.tunnel_url = url
                self.after(0, lambda u=url: (self.tunnel_lbl.config(text=u, foreground="#4fd1c5"),
                                             self.log(f"[+] Quick tunnel: {u}  (give this to remote devices)")))
        self.after(0, self._tunnel_ended)

    def _tunnel_ended(self):
        self.tunnel_proc = None
        self.tunnel_btn.config(text="Start quick tunnel")
        if not self.tunnel_url:
            self.tunnel_lbl.config(text="(off)", foreground="#8b98a5")

    def stop_tunnel(self):
        if self.tunnel_proc:
            try:
                self.tunnel_proc.terminate()
            except Exception:
                pass
            self.tunnel_proc = None
        self.tunnel_url = None
        self.tunnel_btn.config(text="Start quick tunnel")
        self.tunnel_lbl.config(text="(off)", foreground="#8b98a5")
        self.log("[i] Tunnel stopped.")

    # ---- settings ----
    def pick_folder(self):
        p = filedialog.askdirectory(initialdir=self.store_var.get() or hub.BASE)
        if p:
            self.store_var.set(p); self.save_settings()
            if self.hub:
                self.hub.set_captures_dir(p)
            self.log(f"[i] Storage folder: {p}")

    def open_folder(self):
        p = self.store_var.get()
        os.makedirs(p, exist_ok=True)
        try:
            os.startfile(p)  # Windows
        except Exception:
            webbrowser.open("file://" + p)

    def change_output(self):
        idx = self.out_combo.current()
        _, dev = self._out_devs[idx]
        self.cfg["output_device"] = dev
        self.engine.device = dev
        try:
            self.engine.start()  # restart with new device
            self.log(f"[i] Output device: {self.out_combo.get()}")
        except Exception as e:
            self.log(f"[-] Output device failed: {e}")
        self.save_settings()

    def save_settings(self):
        self.cfg.update({
            "port": int(self.port_var.get()),
            "storage_dir": self.store_var.get(),
            "output_device": self.cfg["output_device"],
            "out_sr": self.engine.out_sr,
            "auto_record": bool(self.auto_var.get()),
            "filename_template": self.fname_var.get(),
            "dsp_settings": self.dsp_settings,
        })
        save_config(self.cfg)
        if self.hub:
            self.hub.set_captures_dir(self.store_var.get())

    # ---- selection / vol ----
    def _sync_selection(self):
        sid = self._selected_sid()
        on = sid is not None
        self.listen_btn.config(state="normal" if on else "disabled")
        self.record_btn.config(state="normal" if on else "disabled")
        rec = self.active.get(sid) if on else None
        self.listen_btn.config(text="■ Stop listen" if rec and rec["listen"] else "▶ Listen")
        self.record_btn.config(text="■ Stop rec" if rec and rec["record"] else "● Record")
        if rec:
            self.vol_var.set(rec["ds"].vol * 100)
        self._load_dsp_panel()

    def _apply_vol(self):
        sid = self._selected_sid()
        rec = self.active.get(sid) if sid is not None else None
        if rec:
            rec["ds"].vol = self.vol_var.get() / 100.0

    # ---- per-device enhancement ----
    @staticmethod
    def _default_dsp():
        return {"enabled": True, "agc": True, "gate": True, "hp": True,
                "gain": 12.0, "sens": 34.0, "gate_db": -58.0}

    def _apply_settings_to_ds(self, ds, s):
        d = ds.dsp
        d.enabled = bool(s["enabled"]); d.agc = bool(s["agc"])
        d.gate = bool(s["gate"]); d.highpass = bool(s["hp"])
        d.gain = db_to_lin(s["gain"])
        d.agc_max_gain = db_to_lin(s["sens"])
        d.gate_thresh = db_to_lin(s["gate_db"])

    def _selected_name(self):
        sid = self._selected_sid()
        d = self._dev_info(sid) if sid is not None else None
        return d["name"] if d else None

    def _on_dsp_change(self):
        self.e_gain_lbl.config(text=f"+{self.e_gain.get():.0f} dB")
        self.e_sens_lbl.config(text=f"+{self.e_sens.get():.0f} dB")
        self.e_gate_lbl.config(text=f"{self.e_gatedb.get():.0f} dBFS")
        if self._loading_dsp:
            return
        name = self._selected_name()
        if not name:
            return
        s = {"enabled": self.e_enh.get(), "agc": self.e_agc.get(), "gate": self.e_gate.get(),
             "hp": self.e_hp.get(), "gain": self.e_gain.get(), "sens": self.e_sens.get(),
             "gate_db": self.e_gatedb.get()}
        self.dsp_settings[name] = s
        rec = self.active.get(self._selected_sid())
        if rec:
            self._apply_settings_to_ds(rec["ds"], s)

    def _load_dsp_panel(self):
        name = self._selected_name()
        if not name:
            self.enh_which.config(text="(no device selected)")
            return
        self.enh_which.config(text=name)
        s = self.dsp_settings.get(name) or self._default_dsp()
        self._loading_dsp = True
        self.e_enh.set(s["enabled"]); self.e_agc.set(s["agc"])
        self.e_gate.set(s["gate"]); self.e_hp.set(s["hp"])
        self.e_gain.set(s["gain"]); self.e_sens.set(s["sens"]); self.e_gatedb.set(s["gate_db"])
        self.e_gain_lbl.config(text=f"+{s['gain']:.0f} dB")
        self.e_sens_lbl.config(text=f"+{s['sens']:.0f} dB")
        self.e_gate_lbl.config(text=f"{s['gate_db']:.0f} dBFS")
        self._loading_dsp = False

    # ---- periodic refresh ----
    def _tick(self):
        try:
            self._refresh_rows()
        except Exception as e:
            # never silently: a swallowed error here is invisible and the UI
            # just appears to stop updating
            if getattr(self, "_last_tick_err", None) != str(e):
                self._last_tick_err = str(e)
                self.log(f"[-] Refresh error: {e}")
        try:
            self._watch_engine()
        except Exception:
            pass
        self.after(400, self._tick)

    def _watch_engine(self):
        """Restart the output stream if its callback has died.

        Listening locally depends on this one sounddevice stream; the browser
        monitor does not, which is why a dead stream shows up as 'the monitor
        works but the app is silent'."""
        eng = self.engine
        if eng.cb_errors and eng.last_error != getattr(self, "_last_eng_err", None):
            self._last_eng_err = eng.last_error
            self.log(f"[-] Output callback error ({eng.cb_errors}x): {eng.last_error}")
        if eng.playing_count() == 0 or eng.stream is None:
            return
        if eng.healthy():
            self._eng_dead_since = None
            return
        if getattr(self, "_eng_dead_since", None) is None:
            self._eng_dead_since = time.monotonic()
            return
        if time.monotonic() - self._eng_dead_since < 1.5:
            return
        self._eng_dead_since = None
        self.log("[!] Output stream stopped responding - restarting it.")
        try:
            eng.start()
            self.log("[+] Output stream restarted.")
        except Exception as e:
            self.log(f"[-] Could not restart output: {e}")

    def _refresh_rows(self):
        if not self.hub:
            return
        remote = {d["id"]: d for d in self.hub.snapshot_devices()}

        # auto-record newly seen REMOTE devices
        if self.auto_var.get():
            for sid, d in remote.items():
                if sid not in self.active:
                    rec = self._ensure_active(sid, d["name"], d["sr"])
                    rec["listen"] = True; rec["ds"].playing = True
                    path = self._make_path(d); rec["ds"].start_record(path); rec["record"] = True
                    self.log(f"[auto] listening+recording {d['name']} -> {path}")

        # combined view: remote devices + added local devices
        combined = {sid: dict(d, local=False) for sid, d in remote.items()}
        for key, rec in self.active.items():
            if rec.get("kind") == "local":
                s = rec["src"]
                combined[key] = {"id": key, "name": s.name, "sr": s.dev_sr,
                                 "level": {"rms": s.ds.level_rms}, "local": True}

        # remove rows no longer present
        for key in list(self.row_iid.keys()):
            if key not in combined:
                self.tree.delete(self.row_iid.pop(key))
                rec = self.active.get(key)
                if rec and rec.get("kind") == "remote":
                    if rec["record"]:
                        self.log(f"[+] Saved (device left): {rec['ds'].stop_record()}")
                    rec["feeder"].stop(); self.engine.remove(key); self.active.pop(key, None)

        # add/update rows
        for key, d in combined.items():
            rec = self.active.get(key)
            lvl = d["level"]["rms"]
            db = f"{20*math.log10(lvl):.0f} dBFS" if lvl > 1e-6 else "—"
            name = d["name"] + (" ·local" if d.get("local") else "")
            listening = "yes" if rec and rec["listen"] else ""
            recording = "● REC" if rec and rec["record"] else ""
            vals = (name, d["id"], f"{d['sr']} Hz", db, listening, recording)
            if key in self.row_iid:
                self.tree.item(self.row_iid[key], values=vals)
            else:
                self.row_iid[key] = self.tree.insert("", "end", values=vals)

        if self.engine.clip:
            self.engine.clip = 0

    # ---- misc ----
    def open_monitor(self):
        if self.hub:
            webbrowser.open(self.monitor_url)

    def copy_sender(self):
        if self.hub:
            self.clipboard_clear(); self.clipboard_append(self.sender_url)
            self.log("[i] Sender URL (LAN) copied.")

    def copy_tunnel(self):
        if self.tunnel_url:
            self.clipboard_clear(); self.clipboard_append(self.tunnel_url)
            self.log(f"[i] Tunnel URL copied: {self.tunnel_url}")
        else:
            self.log("[-] No tunnel URL yet — start a tunnel first (and wait for the URL).")

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", f"{ts}  {msg}\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def on_close(self):
        try:
            self.save_settings()   # persist per-device enhancement + settings
            self.stop_tunnel()
            self.stop_all(); self.engine.stop()
            if self.hub:
                self.hub.stop()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
