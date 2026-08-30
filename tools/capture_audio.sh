#!/bin/bash
# tools/capture_audio.sh — Linux/Docker helper for HCI-level capture with btmon
#
# Captures your local adapter's HCI traffic (the connections it is party to)
# to a Bluetooth snoop log you can open in Wireshark. Own-device / authorized.
#
# Usage: sudo ./tools/capture_audio.sh [seconds] [outfile]

DURATION="${1:-60}"
OUT="${2:-data/captures/hci_$(date +%Y%m%d_%H%M%S).snoop}"

mkdir -p data/captures

echo "[*] Capturing HCI traffic for ${DURATION}s -> ${OUT}"
echo "[*] (Open the resulting .snoop in Wireshark; filter with config/filters.conf)"

# btmon writes a BT snoop log Wireshark understands.
timeout "${DURATION}" btmon -w "${OUT}" || true

echo "[+] Saved: ${OUT}"
