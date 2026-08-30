#!/usr/bin/env python3
# src/audio_capture.py
#
# NOTE: SCO capture uses Linux BlueZ sockets (AF_BLUETOOTH / BTPROTO_SCO).
# This will not run on Windows. It only observes audio on connections your
# own adapter is party to; it does not defeat link-layer encryption.
# Authorized / own-device testing only.

import socket
import logging
import os
from datetime import datetime

import numpy as np


class AudioStreamAnalyzer:
    """Analyze Bluetooth SCO audio streams on connections you control"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.audio_buffer = []
        self.capture_active = False
        self.audio_stats = {}
        self.sco_socket = None

    def setup_sco_socket(self, target_mac, channel=1):
        """Setup SCO socket for audio capture (Linux only)"""
        print(f"[*] Setting up SCO socket to {target_mac} on channel {channel}")

        if not hasattr(socket, "AF_BLUETOOTH"):
            self.logger.error("AF_BLUETOOTH unavailable — Linux/BlueZ required")
            print("[-] SCO sockets not supported on this platform")
            return False

        try:
            self.sco_socket = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_SEQPACKET,
                socket.BTPROTO_SCO,
            )
            self.sco_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            print("[+] SCO socket created")
            return True
        except Exception as e:
            self.logger.error(f"SCO socket setup failed: {e}")
            return False

    def capture_audio_stream(self, duration=30):
        """Capture audio stream for analysis"""
        if self.sco_socket is None:
            print("[-] No SCO socket — call setup_sco_socket() first")
            return
        print(f"[*] Starting audio capture for {duration} seconds...")
        self.capture_active = True
        start_time = datetime.now()

        while self.capture_active and (datetime.now() - start_time).seconds < duration:
            try:
                data = self.sco_socket.recv(1024)
                if data:
                    self.audio_buffer.append(data)
                    self.analyze_audio_packet(data)
            except Exception as e:
                self.logger.error(f"Capture error: {e}")
                break

        self.capture_active = False
        print(f"[+] Captured {len(self.audio_buffer)} audio packets")

    def analyze_audio_packet(self, packet_data):
        """Analyze individual audio packet for basic signal statistics"""
        try:
            audio_samples = np.frombuffer(packet_data, dtype=np.int16)
            if audio_samples.size == 0:
                return

            stats = {
                "sample_count": int(len(audio_samples)),
                "rms_amplitude": float(np.sqrt(np.mean(audio_samples.astype(np.float64) ** 2))),
                "peak_amplitude": float(np.max(np.abs(audio_samples))),
                "zero_crossings": int(np.sum(np.diff(np.signbit(audio_samples)))),
                "timestamp": datetime.now().isoformat(),
            }

            self.audio_stats[len(self.audio_buffer)] = stats

            if stats["rms_amplitude"] > 1000:  # crude voice-activity threshold
                print(f"[+] Voice activity detected - RMS: {stats['rms_amplitude']:.2f}")
        except Exception as e:
            self.logger.debug(f"Analysis error: {e}")

    def save_audio_data(self, filename="captured_audio.raw"):
        """Save captured raw audio data"""
        os.makedirs("data/captures", exist_ok=True)
        path = os.path.join("data", "captures", filename)
        with open(path, "wb") as f:
            for packet in self.audio_buffer:
                f.write(packet)
        print(f"[+] Audio data saved to {path}")

    def analyze_frequency_content(self, sample_rate=16000):
        """Analyze frequency content of captured audio via FFT"""
        if not self.audio_buffer:
            print("[-] No audio data to analyze")
            return None

        combined_data = b"".join(self.audio_buffer)
        samples = np.frombuffer(combined_data, dtype=np.int16)
        if samples.size == 0:
            return None

        fft_result = np.fft.fft(samples)
        freqs = np.fft.fftfreq(len(samples), 1 / sample_rate)
        magnitude = np.abs(fft_result)
        dominant_indices = np.argsort(magnitude)[-10:]

        return {
            "dominant_frequencies": [
                {"frequency": float(abs(freqs[i])), "magnitude": float(magnitude[i])}
                for i in dominant_indices
            ],
            "total_samples": int(len(samples)),
            "sample_rate": sample_rate,
            "duration": float(len(samples) / sample_rate),
        }
