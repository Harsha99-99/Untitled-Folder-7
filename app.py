#!/usr/bin/env python3
# app.py — Bluetooth Audio Monitor (Windows GUI)
#
# Listens to the audio stream of a CONNECTED / PAIRED Bluetooth device by
# capturing the microphone endpoint that Windows exposes for it (the
# "Hands-Free" input of e.g. OnePlus Buds Pro 2), with a real-time voice
# enhancement chain tuned for human conversation:
#   high-pass -> gain -> AGC (auto boost faint speech) -> noise gate -> limiter
#
# QUALITY CEILING (important): the earbuds' mic reaches Windows over Bluetooth
# Hands-Free Profile, which is capped by the BT spec at 8 kHz (CVSD) or 16 kHz
# (mSBC / HD-Voice). No software can produce true hi-fi (44.1 kHz+) from that —
# those frequencies are never transmitted. A2DP is hi-fi but playback-only and
# carries no mic. The DSP below maximises intelligibility within that ceiling.
#
# Uses the standard OS audio stack (WASAPI via PortAudio) and only endpoints
# Windows already exposes for YOUR paired device. Own-device / authorized use.

import os
import queue
import re
import threading
from datetime import datetime

import numpy as np
import sounddevice as sd
from scipy.signal import butter, lfilter

import tkinter as tk
from tkinter import ttk, messagebox

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

CAPTURES_DIR = os.path.join("data", "captures")
BLOCK = 1024
RING_SECONDS = 3.0


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------
def hostapi_name(index):
    try:
        return sd.query_hostapis(index)["name"]
    except Exception:
        return "?"


def looks_bluetooth(name: str) -> bool:
    n = name.lower()
    return any(
        k in n
        for k in ("hands-free", "headset", "bluetooth", "buds", "airpods", "hfp", "bthhfenum")
    )


# Windows exposes Bluetooth Hands-Free (HFP) capture endpoints through
# bthhfenum.sys under a raw resource string that is unreadable in a device
# list (it even embeds a newline), so recover the friendly name from it.
_HFP_DEV_RE = re.compile(r";\(\s*([^()]+?)\s*\)\s*\)?\s*$")
_HFP_PROFILE_RE = re.compile(r"%1\s*([^%]+?)\s*%0")


def pretty_input_name(raw):
    """'Headset (@...bthhfenum.sys,#2;%1 Hands-Free%0 ;(boAt Stone 650))'
    -> 'boAt Stone 650 (Hands-Free)'. Other names pass through unchanged."""
    flat = " ".join(str(raw).split())
    m = _HFP_DEV_RE.search(flat)
    if not m:
        return flat
    prof = _HFP_PROFILE_RE.search(flat)
    return f"{m.group(1)} ({prof.group(1) if prof else 'Hands-Free'})"


def list_input_devices():
    """Return [(index, label, is_bt)] for devices with input channels."""
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        api = hostapi_name(dev["hostapi"])
        label = f"[{idx}] {pretty_input_name(dev['name'])}  ({api})"
        out.append((idx, label, looks_bluetooth(dev["name"])))
    return out


def default_output_index():
    try:
        d = sd.default.device
        if isinstance(d, (list, tuple)) and d[1] is not None and d[1] >= 0:
            return d[1]
    except Exception:
        pass
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0:
            return idx
    return None


def negotiated_samplerate(in_index, in_ch):
    """The device's real (driver-negotiated) rate — for HFP this is the true
    8k/16k ceiling. We report it rather than upsampling to fake a higher rate."""
    try:
        dev = sd.query_devices(in_index)
        sr = int(round(dev.get("default_samplerate") or 0))
        if sr > 0:
            return sr
    except Exception:
        pass
    return 16000


# --------------------------------------------------------------------------
# Real-time voice DSP (block-based, stateful)
# --------------------------------------------------------------------------
class VoiceDSP:
    """High-pass -> gain -> AGC -> noise gate -> soft limiter.

    Designed for faint/at-a-distance human speech within the HFP ceiling.
    All stages keep state across blocks so there are no per-block artifacts.
    """

    def __init__(self, samplerate):
        self.sr = samplerate
        self.enabled = True

        # manual makeup gain (linear); UI sets via dB
        self.gain = db_to_lin(12.0)

        # AGC — the "supersensitive" stage: pull quiet speech toward a target
        self.agc = True
        self.agc_target = db_to_lin(-18.0)      # target RMS
        self.agc_max_gain = db_to_lin(34.0)     # how hard it may boost
        self._agc_gain = 1.0

        # noise gate — silence the hiss between words (kept gentle = sensitive)
        self.gate = True
        self.gate_thresh = db_to_lin(-58.0)     # open above this input RMS
        self._gate_env = 0.0

        # high-pass — remove rumble / DC / handling noise
        self.highpass = True
        self._build_hp(110.0)

    def _build_hp(self, fc):
        ny = self.sr / 2.0
        fc = min(fc, ny * 0.9)
        self._b, self._a = butter(2, fc / ny, btype="high")
        self._zi = np.zeros(max(len(self._a), len(self._b)) - 1, dtype=np.float64)

    def rebuild(self, samplerate):
        self.sr = samplerate
        self._build_hp(110.0)
        self._agc_gain = 1.0
        self._gate_env = 0.0

    def process(self, x):
        if not self.enabled:
            return x
        y = x.astype(np.float64)

        if self.highpass:
            y, self._zi = lfilter(self._b, self._a, y, zi=self._zi)

        y = y * self.gain

        raw_rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) + 1e-9

        if self.agc:
            cur_rms = float(np.sqrt(np.mean(y ** 2))) + 1e-9
            desired = min(self.agc_target / cur_rms, self.agc_max_gain)
            # Don't ramp gain UP during (near-)silence — that just amplifies the
            # noise floor in pauses. Only boost when signal is above the gate.
            below_gate = self.gate and (raw_rms <= self.gate_thresh)
            if below_gate and desired > self._agc_gain:
                desired = self._agc_gain  # hold
            # attack faster than release for responsiveness on speech onsets
            coeff = 0.25 if desired < self._agc_gain else 0.12
            self._agc_gain = (1 - coeff) * self._agc_gain + coeff * desired
            y = y * self._agc_gain

        if self.gate:
            target = 1.0 if raw_rms > self.gate_thresh else 0.0
            coeff = 0.30 if target >= self._gate_env else 0.06  # fast open, slow close
            self._gate_env = (1 - coeff) * self._gate_env + coeff * target
            y = y * self._gate_env

        # soft limiter: linear below 0.7, gentle knee above (boost w/o hard clip)
        a = np.abs(y)
        knee = a > 0.7
        y[knee] = np.sign(y[knee]) * (0.7 + 0.3 * np.tanh((a[knee] - 0.7) / 0.3))
        return y.astype(np.float32)


def db_to_lin(db):
    return float(10.0 ** (db / 20.0))


# --------------------------------------------------------------------------
# Monitor buffer (decouples exclusive input stream from shared output stream)
# --------------------------------------------------------------------------
class MonoBuffer:
    def __init__(self, maxlen):
        self.buf = np.zeros(0, dtype=np.float32)
        self.maxlen = maxlen
        self.lock = threading.Lock()

    def push(self, x):
        with self.lock:
            self.buf = np.concatenate([self.buf, x.astype(np.float32)])
            if self.buf.size > self.maxlen:  # bound latency: drop oldest
                self.buf = self.buf[-self.maxlen:]

    def pop(self, n):
        with self.lock:
            if self.buf.size >= n:
                out = self.buf[:n]
                self.buf = self.buf[n:]
                return out
            out = np.zeros(n, dtype=np.float32)
            out[: self.buf.size] = self.buf
            self.buf = np.zeros(0, dtype=np.float32)
            return out


# --------------------------------------------------------------------------
# Audio engine
# --------------------------------------------------------------------------
class AudioEngine:
    def __init__(self, log):
        self.log = log
        self.in_stream = None
        self.out_stream = None
        self.samplerate = 16000
        self.in_channels = 1
        self.out_channels = 2
        self.want_exclusive = True

        self.lock = threading.Lock()
        self.ring = np.zeros(1, dtype=np.float32)
        self.level_rms = 0.0
        self.level_peak = 0.0

        self.recording = False
        self._rec_frames = []          # enhanced mono mix
        self._rec_multi = []           # raw per-channel (for L/R separation)
        self.rec_separate = True
        self.monitor = False
        self.running = False
        self._err_q = queue.Queue()
        self._in_dtype = "float32"
        self._monbuf = None

        self.dsp = VoiceDSP(self.samplerate)

    # --- open the capture stream, bypassing the shared mixer if possible --
    def _open_input(self, in_index, sr, in_ch):
        """Try, in order:
        1. WASAPI exclusive (float32, then int16) — bypasses the shared mixer
        2. Native open on the device's host API (WDM-KS already bypasses it)
        3. Shared float32 fallback
        Returns (stream, dtype, description).
        """
        dev = sd.query_devices(in_index)
        api = hostapi_name(dev["hostapi"]).lower()
        attempts = []

        if self.want_exclusive and "wasapi" in api:
            try:
                ex = sd.WasapiSettings(exclusive=True)
                attempts.append(("float32", ex, 0, "WASAPI EXCLUSIVE (f32) — mixer bypassed"))
                attempts.append(("int16", ex, 0, "WASAPI EXCLUSIVE (int16) — mixer bypassed"))
            except Exception as e:
                self.log(f"[i] WASAPI exclusive unavailable: {e}")

        # native open (no extra settings). For WDM-KS this is kernel streaming,
        # which also does not pass through the Windows shared mixer.
        native_note = "kernel-streaming (mixer bypassed)" if "wdm-ks" in api or "ks" in api \
            else "native/shared"
        attempts.append(("float32", None, 0, f"{api} {native_note} (f32)"))
        attempts.append(("int16", None, 0, f"{api} {native_note} (int16)"))

        last = None
        for dtype, extra, bs, desc in attempts:
            try:
                st = sd.InputStream(
                    samplerate=sr, blocksize=bs, device=in_index,
                    channels=in_ch, dtype=dtype, latency="low",
                    extra_settings=extra, callback=self._input_cb,
                )
                st.start()
                return st, dtype, desc
            except Exception as e:
                last = e
                continue
        raise last if last else RuntimeError("no input open attempt succeeded")

    # --- lifecycle -------------------------------------------------------
    def start(self, in_index, monitor=True):
        if self.running:
            self.stop()

        dev = sd.query_devices(in_index)
        sr = negotiated_samplerate(in_index, 1)
        dev_ch = int(dev["max_input_channels"])
        in_ch = max(1, min(dev_ch, 8))

        self.samplerate = sr
        self.in_channels = in_ch
        self.monitor = monitor
        self.dsp.rebuild(sr)
        ring_len = max(1, int(RING_SECONDS * sr))
        with self.lock:
            self.ring = np.zeros(ring_len, dtype=np.float32)
        self._monbuf = MonoBuffer(int(0.25 * sr)) if monitor else None

        # report the real ceiling so the user understands the quality limit
        if sr <= 8000:
            band = "narrowband CVSD (8 kHz telephone-grade — the HFP floor)"
        elif sr <= 16000:
            band = "wideband mSBC / HD-Voice (16 kHz — the HFP ceiling)"
        else:
            band = f"{sr} Hz (non-HFP endpoint)"
        self.log(f"[i] Negotiated capture rate: {sr} Hz — {band}")

        if in_ch < 2:
            self.log("[i] MONO endpoint (1 ch) — separate L/R mic recording is "
                     "NOT possible: Bluetooth HFP sends one fused mono uplink. "
                     "Per-channel recording needs a 2+ channel input device.")
        else:
            self.log(f"[i] {in_ch}-channel input — per-channel (L/R…) recording available.")

        try:
            self.in_stream, self._in_dtype, desc = self._open_input(in_index, sr, in_ch)
            self.log(f"[+] Capture path: {desc}  @ {sr} Hz, {in_ch}ch, dev [{in_index}]")
        except Exception as e:
            self.log(f"[-] Failed to open capture: {e}")
            raise

        out_index = default_output_index()
        if monitor and out_index is not None:
            try:
                self.out_channels = 2
                self.out_stream = sd.OutputStream(
                    samplerate=sr, blocksize=0, device=out_index,
                    channels=self.out_channels, dtype="float32",
                    latency="low", callback=self._output_cb,
                )
                self.out_stream.start()
                self.log(f"[+] Monitoring to output [{out_index}]")
            except Exception as e:
                self.log(f"[i] Monitor output unavailable ({e}); capture continues.")
                self.out_stream = None

        self.running = True

    def stop(self):
        self.running = False
        for name in ("in_stream", "out_stream"):
            st = getattr(self, name)
            if st is not None:
                try:
                    st.stop()
                    st.close()
                except Exception:
                    pass
                setattr(self, name, None)
        self._monbuf = None
        self.log("[*] Stopped.")

    # --- callbacks -------------------------------------------------------
    def _to_float(self, indata):
        """Return the full multichannel block as float32 (frames, channels)."""
        if self._in_dtype == "int16":
            arr = indata.astype(np.float32) / 32768.0
        else:
            arr = np.asarray(indata, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        return arr

    def _process(self, indata):
        multi = self._to_float(indata)                              # (frames, ch)
        mono = multi.mean(axis=1) if multi.shape[1] > 1 else multi[:, 0]
        enhanced = self.dsp.process(mono)

        rms = float(np.sqrt(np.mean(enhanced.astype(np.float64) ** 2))) if enhanced.size else 0.0
        peak = float(np.max(np.abs(enhanced))) if enhanced.size else 0.0
        with self.lock:
            n = enhanced.size
            if n >= self.ring.size:
                self.ring = enhanced[-self.ring.size:].astype(np.float32).copy()
            else:
                self.ring = np.roll(self.ring, -n)
                self.ring[-n:] = enhanced
            self.level_rms = rms
            self.level_peak = peak
            if self.recording:
                self._rec_frames.append(enhanced.astype(np.float32).copy())
                if self.rec_separate and multi.shape[1] > 1:
                    self._rec_multi.append(multi.copy())
        if self._monbuf is not None:
            self._monbuf.push(enhanced)
        return enhanced

    def _input_cb(self, indata, frames, time_info, status):
        if status:
            self._err_q.put(str(status))
        self._process(indata)

    def _output_cb(self, outdata, frames, time_info, status):
        if self._monbuf is not None:
            mono = self._monbuf.pop(frames)
        else:
            mono = np.zeros(frames, dtype=np.float32)
        for ch in range(outdata.shape[1]):
            outdata[:, ch] = mono

    # --- recording -------------------------------------------------------
    def start_recording(self):
        with self.lock:
            self._rec_frames = []
            self._rec_multi = []
            self.recording = True

    def stop_recording(self):
        with self.lock:
            self.recording = False
            frames = list(self._rec_frames)
            multi_frames = list(self._rec_multi)
            self._rec_frames = []
            self._rec_multi = []
            sr = self.samplerate
        if not frames:
            return None

        from scipy.io import wavfile
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # enhanced mono mix (float32 — no 16-bit quantization)
        audio = np.concatenate(frames).astype(np.float32)
        path = os.path.join(CAPTURES_DIR, f"btmic_{ts}.wav")
        wavfile.write(path, sr, audio)

        # raw per-channel WAVs (L/R/…) when the device exposes 2+ channels
        channel_paths = []
        if multi_frames:
            multi = np.concatenate(multi_frames, axis=0).astype(np.float32)
            labels = {0: "L", 1: "R"}
            for c in range(multi.shape[1]):
                lbl = labels.get(c, f"ch{c + 1}")
                cp = os.path.join(CAPTURES_DIR, f"btmic_{ts}_{lbl}.wav")
                wavfile.write(cp, sr, multi[:, c].copy())
                channel_paths.append((lbl, cp))

        return path, len(audio) / sr, channel_paths

    def snapshot(self):
        with self.lock:
            return self.ring.copy(), self.level_rms, self.level_peak

    def drain_errors(self):
        msgs = []
        while True:
            try:
                msgs.append(self._err_q.get_nowait())
            except queue.Empty:
                break
        return msgs


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bluetooth Audio Monitor — own-device")
        self.geometry("940x760")
        self.minsize(820, 680)

        self.engine = AudioEngine(self.log)
        self.devices = []
        self._build_ui()
        self.refresh_devices()
        self._tick()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- layout ----------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Input device:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            top, textvariable=self.device_var, width=64, state="readonly"
        )
        self.device_combo.pack(side="left", padx=6)
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(side="left")

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)
        self.listen_btn = ttk.Button(ctrl, text="▶  Listen", command=self.toggle_listen)
        self.listen_btn.pack(side="left")
        self.monitor_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="Play to speakers", variable=self.monitor_var
        ).pack(side="left", padx=12)
        self.excl_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="Bypass mixer (exclusive/KS)", variable=self.excl_var
        ).pack(side="left", padx=4)
        self.rec_btn = ttk.Button(
            ctrl, text="●  Record", command=self.toggle_record, state="disabled"
        )
        self.rec_btn.pack(side="left", padx=6)
        self.sep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="Separate L/R channels", variable=self.sep_var
        ).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Analyze last", command=self.analyze_last).pack(
            side="left", padx=6
        )
        ttk.Button(ctrl, text="Scan BLE", command=self.scan_ble).pack(side="left", padx=6)

        # --- voice enhancement panel ---
        enh = ttk.LabelFrame(self, text="Voice enhancement (for faint/distant speech)")
        enh.pack(fill="x", **pad)

        row1 = ttk.Frame(enh)
        row1.pack(fill="x", padx=8, pady=4)
        self.enh_var = tk.BooleanVar(value=True)
        self.agc_var = tk.BooleanVar(value=True)
        self.gate_var = tk.BooleanVar(value=True)
        self.hp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="Enhance", variable=self.enh_var,
                        command=self._apply_dsp).pack(side="left", padx=4)
        ttk.Checkbutton(row1, text="AGC (auto-boost)", variable=self.agc_var,
                        command=self._apply_dsp).pack(side="left", padx=4)
        ttk.Checkbutton(row1, text="Noise gate", variable=self.gate_var,
                        command=self._apply_dsp).pack(side="left", padx=4)
        ttk.Checkbutton(row1, text="High-pass", variable=self.hp_var,
                        command=self._apply_dsp).pack(side="left", padx=4)

        row2 = ttk.Frame(enh)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="Gain", width=6).pack(side="left")
        self.gain_var = tk.DoubleVar(value=12.0)
        ttk.Scale(row2, from_=0, to=40, variable=self.gain_var, length=180,
                  command=lambda e: self._apply_dsp()).pack(side="left", padx=4)
        self.gain_lbl = ttk.Label(row2, text="+12 dB", width=8)
        self.gain_lbl.pack(side="left")

        ttk.Label(row2, text="Sensitivity", width=10).pack(side="left", padx=(16, 0))
        self.sens_var = tk.DoubleVar(value=34.0)
        ttk.Scale(row2, from_=0, to=48, variable=self.sens_var, length=180,
                  command=lambda e: self._apply_dsp()).pack(side="left", padx=4)
        self.sens_lbl = ttk.Label(row2, text="+34 dB", width=8)
        self.sens_lbl.pack(side="left")

        row3 = ttk.Frame(enh)
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Label(row3, text="Gate threshold", width=14).pack(side="left")
        self.gate_var_db = tk.DoubleVar(value=-58.0)
        ttk.Scale(row3, from_=-80, to=-30, variable=self.gate_var_db, length=180,
                  command=lambda e: self._apply_dsp()).pack(side="left", padx=4)
        self.gate_lbl = ttk.Label(row3, text="-58 dBFS", width=10)
        self.gate_lbl.pack(side="left")

        # level meter
        meter = ttk.Frame(self)
        meter.pack(fill="x", **pad)
        ttk.Label(meter, text="Level:").pack(side="left")
        self.level_bar = ttk.Progressbar(meter, maximum=100, length=420)
        self.level_bar.pack(side="left", padx=8)
        self.level_lbl = ttk.Label(meter, text="-inf dBFS", width=22)
        self.level_lbl.pack(side="left")

        # waveform
        self.fig = Figure(figsize=(8, 2.4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_ylim(-1, 1)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.ax.set_title("Live waveform (post-enhancement)")
        (self.line,) = self.ax.plot(np.zeros(1024), linewidth=0.7)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=6)

        # log
        self.log_txt = tk.Text(self, height=7, wrap="word")
        self.log_txt.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log_txt.configure(state="disabled")

        self.log(
            "Ceiling: the earbuds' mic uses Bluetooth Hands-Free Profile (8 kHz "
            "CVSD or 16 kHz mSBC). That caps fidelity — enhancement below maximizes "
            "intelligibility within it. Own-device use only."
        )
        self._apply_dsp()

    # --- DSP binding -----------------------------------------------------
    def _apply_dsp(self):
        d = self.engine.dsp
        d.enabled = self.enh_var.get()
        d.agc = self.agc_var.get()
        d.gate = self.gate_var.get()
        d.highpass = self.hp_var.get()
        d.gain = db_to_lin(self.gain_var.get())
        d.agc_max_gain = db_to_lin(self.sens_var.get())
        d.gate_thresh = db_to_lin(self.gate_var_db.get())
        self.gain_lbl.config(text=f"+{self.gain_var.get():.0f} dB")
        self.sens_lbl.config(text=f"+{self.sens_var.get():.0f} dB")
        self.gate_lbl.config(text=f"{self.gate_var_db.get():.0f} dBFS")

    # --- actions ---------------------------------------------------------
    def refresh_devices(self):
        self.devices = list_input_devices()
        labels = []
        bt_default = None
        for i, (idx, label, is_bt) in enumerate(self.devices):
            tag = "  ★BT" if is_bt else ""
            labels.append(label + tag)
            if is_bt and bt_default is None:
                bt_default = i
        self.device_combo["values"] = labels
        if labels:
            self.device_combo.current(bt_default if bt_default is not None else 0)
        self.log(f"[*] Found {len(labels)} input endpoints "
                 f"({sum(1 for _,_,b in self.devices if b)} look Bluetooth).")

    def _selected_index(self):
        sel = self.device_combo.current()
        if sel < 0 or sel >= len(self.devices):
            return None
        return self.devices[sel][0]

    def toggle_listen(self):
        if self.engine.running:
            self.engine.stop()
            self.listen_btn.config(text="▶  Listen")
            self.rec_btn.config(state="disabled")
            return
        idx = self._selected_index()
        if idx is None:
            messagebox.showwarning("No device", "Select an input device first.")
            return
        self._apply_dsp()
        self.engine.want_exclusive = self.excl_var.get()
        try:
            self.engine.start(idx, monitor=self.monitor_var.get())
            self.listen_btn.config(text="■  Stop")
            self.rec_btn.config(state="normal")
        except Exception as e:
            messagebox.showerror("Stream error", str(e))

    def toggle_record(self):
        if not self.engine.running:
            return
        if self.engine.recording:
            result = self.engine.stop_recording()
            self.rec_btn.config(text="●  Record")
            if result:
                path, dur, channel_paths = result
                self.log(f"[+] Saved mono mix: {path}  ({dur:.1f}s, float32)")
                self._last_recording = path
                for lbl, cp in channel_paths:
                    self.log(f"[+] Saved {lbl} channel: {cp}")
                if self.sep_var.get() and not channel_paths and self.engine.in_channels < 2:
                    self.log("[i] Only the mono mix was saved — this endpoint has "
                             "1 channel, so there is no separate L/R to split.")
            else:
                self.log("[-] Nothing recorded.")
        else:
            self.engine.rec_separate = self.sep_var.get()
            self.engine.start_recording()
            self.rec_btn.config(text="■  Stop rec")
            self.log("[*] Recording…")

    def analyze_last(self):
        path = getattr(self, "_last_recording", None)
        if not path or not os.path.exists(path):
            messagebox.showinfo("Analyze", "Record something first.")
            return

        def worker():
            try:
                import sys
                sys.path.insert(0, "src")
                from analyzer import AudioAnalyzer
                a = AudioAnalyzer()
                res = a.analyze_captured_audio(path)
                png = a.generate_visualization(path)
                va = res["voice_activity"]
                self.log(
                    f"[analysis] dur={res['duration']:.1f}s "
                    f"voice={va['likely_contains_voice']} "
                    f"centroid={res['frequency_content']['spectral_centroid']:.0f}Hz "
                    f"SNR={res['noise_level']['snr_estimate']:.1f}"
                )
                self.log(f"[analysis] plot: {png}")
            except Exception as e:
                self.log(f"[-] Analysis error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def scan_ble(self):
        def worker():
            try:
                import asyncio, sys
                sys.path.insert(0, "src")
                from ble_connect import scan
                self.log("[*] BLE scanning 8s…")
                res = asyncio.run(scan(8.0))
                for e in res[:12]:
                    self.log(f"    {e['name'] or '(no name)'}  {e['address']}  RSSI={e['rssi']}")
            except Exception as e:
                self.log(f"[-] BLE scan error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # --- periodic UI update ---------------------------------------------
    def _tick(self):
        if self.engine.running:
            ring, rms, peak = self.engine.snapshot()
            step = max(1, ring.size // 1024)
            y = ring[::step][:1024]
            if y.size < 1024:
                y = np.pad(y, (0, 1024 - y.size))
            self.line.set_ydata(y)
            self.line.set_xdata(np.arange(y.size))
            self.ax.set_xlim(0, y.size)
            self.canvas.draw_idle()

            db = 20 * np.log10(rms) if rms > 1e-6 else -120
            self.level_bar["value"] = max(0, min(100, (db + 60) / 60 * 100))
            self.level_lbl.config(text=f"{db:6.1f} dBFS  peak {peak:4.2f}")

            for m in self.engine.drain_errors():
                self.log(f"[stream] {m}")
        self.after(60, self._tick)

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", f"{ts}  {msg}\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def on_close(self):
        try:
            self.engine.stop()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
