#!/usr/bin/env python3
# tools/analyze_results.py
#
# Convenience CLI over src/analyzer.py — analyze a .wav capture and emit a
# JSON summary + waveform/spectrum PNG. Cross-platform.

import argparse
import json
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from analyzer import AudioAnalyzer  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Analyze a captured WAV file")
    parser.add_argument("wav", help="Path to a .wav file")
    parser.add_argument("--no-plot", action="store_true", help="Skip PNG generation")
    args = parser.parse_args()

    analyzer = AudioAnalyzer()
    result = analyzer.analyze_captured_audio(args.wav)
    if result is None:
        print("[-] Analysis failed.")
        sys.exit(1)

    print(json.dumps(result, indent=2))

    os.makedirs("data/analysis", exist_ok=True)
    with open("data/analysis/last_analysis.json", "w") as f:
        json.dump(result, f, indent=2)

    if not args.no_plot:
        analyzer.generate_visualization(args.wav)


if __name__ == "__main__":
    main()
