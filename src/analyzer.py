#!/usr/bin/env python3
# src/analyzer.py
#
# Pure signal-analysis of audio files you already have. This module is
# cross-platform (numpy/scipy/matplotlib) and runs fine on Windows.

import os
import logging
from datetime import datetime

import numpy as np
import matplotlib

matplotlib.use("Agg")  # headless-safe
import matplotlib.pyplot as plt
from scipy import signal
from scipy.io import wavfile


class AudioAnalyzer:
    """Advanced audio analysis tools for captured .wav files"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.analysis_results = {}

    def analyze_captured_audio(self, audio_file):
        """Analyze a captured audio (.wav) file"""
        print(f"[*] Analyzing audio file: {audio_file}")
        try:
            sample_rate, audio_data = wavfile.read(audio_file)
            analysis = {
                "sample_rate": int(sample_rate),
                "duration": float(len(audio_data) / sample_rate),
                "channels": len(audio_data.shape) if len(audio_data.shape) > 1 else 1,
                "amplitude_stats": self.calculate_amplitude_stats(audio_data),
                "frequency_content": self.analyze_frequency_content(audio_data, sample_rate),
                "voice_activity": self.detect_voice_activity(audio_data, sample_rate),
                "noise_level": self.calculate_noise_level(audio_data),
            }
            self.analysis_results = analysis
            return analysis
        except Exception as e:
            self.logger.error(f"Analysis failed: {e}")
            return None

    def calculate_amplitude_stats(self, audio_data):
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)
        audio_data = audio_data.astype(np.float64)
        rms = float(np.sqrt(np.mean(audio_data ** 2)))
        return {
            "min": float(np.min(audio_data)),
            "max": float(np.max(audio_data)),
            "mean": float(np.mean(audio_data)),
            "rms": rms,
            "peak_to_peak": float(np.ptp(audio_data)),
            "crest_factor": float(np.max(np.abs(audio_data)) / rms) if rms > 0 else 0.0,
        }

    def analyze_frequency_content(self, audio_data, sample_rate):
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)
        audio_data = audio_data.astype(np.float64)

        fft_result = np.fft.fft(audio_data)
        freqs = np.fft.fftfreq(len(audio_data), 1 / sample_rate)
        power_spectrum = np.abs(fft_result) ** 2

        half = len(freqs) // 2
        positive_freqs = freqs[:half]
        positive_power = power_spectrum[:half]
        total_power = np.sum(positive_power)
        dominant_indices = np.argsort(positive_power)[-10:]

        centroid = float(np.sum(positive_freqs * positive_power) / total_power) if total_power > 0 else 0.0
        bandwidth = (
            float(
                np.sqrt(
                    np.sum(((positive_freqs - centroid) ** 2) * positive_power) / total_power
                )
            )
            if total_power > 0
            else 0.0
        )

        return {
            "dominant_frequencies": [
                {"frequency": float(positive_freqs[i]), "power": float(positive_power[i])}
                for i in dominant_indices
            ],
            "spectral_centroid": centroid,
            "bandwidth": bandwidth,
        }

    def detect_voice_activity(self, audio_data, sample_rate):
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)
        audio_data = audio_data.astype(np.float64)

        voice_band = (300, 3400)
        nyquist = sample_rate / 2
        low = voice_band[0] / nyquist
        high = min(voice_band[1] / nyquist, 0.999)

        try:
            b, a = signal.butter(4, [low, high], btype="band")
            filtered_audio = signal.filtfilt(b, a, audio_data)
        except Exception:
            filtered_audio = audio_data

        voice_energy = float(np.sum(filtered_audio ** 2))
        total_energy = float(np.sum(audio_data ** 2))
        voice_ratio = voice_energy / total_energy if total_energy > 0 else 0.0

        return {
            "voice_band_energy_ratio": voice_ratio,
            "likely_contains_voice": bool(voice_ratio > 0.3),
            "voice_band": list(voice_band),
        }

    def calculate_noise_level(self, audio_data):
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)
        audio_data = audio_data.astype(np.float64)

        noise_floor = float(np.percentile(np.abs(audio_data), 10))
        peak = float(np.max(np.abs(audio_data)))
        return {
            "noise_floor": noise_floor,
            "snr_estimate": (peak / noise_floor) if noise_floor > 0 else float("inf"),
        }

    def generate_visualization(self, audio_file, output_dir="data/analysis/"):
        os.makedirs(output_dir, exist_ok=True)
        sample_rate, audio_data = wavfile.read(audio_file)
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)

        plt.figure(figsize=(12, 8))

        plt.subplot(2, 1, 1)
        time_axis = np.linspace(0, len(audio_data) / sample_rate, len(audio_data))
        plt.plot(time_axis, audio_data)
        plt.title("Audio Waveform")
        plt.xlabel("Time (s)")
        plt.ylabel("Amplitude")

        plt.subplot(2, 1, 2)
        fft_result = np.fft.fft(audio_data)
        freqs = np.fft.fftfreq(len(audio_data), 1 / sample_rate)
        half = len(freqs) // 2
        plt.plot(freqs[:half], np.abs(fft_result[:half]))
        plt.title("Frequency Spectrum")
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("Magnitude")
        plt.xlim(0, sample_rate / 2)

        plt.tight_layout()
        out = os.path.join(
            output_dir, f"audio_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        plt.savefig(out)
        plt.close()
        print(f"[+] Visualization saved to {out}")
        return out


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python analyzer.py <audio.wav>")
    else:
        a = AudioAnalyzer()
        print(a.analyze_captured_audio(sys.argv[1]))
        a.generate_visualization(sys.argv[1])
