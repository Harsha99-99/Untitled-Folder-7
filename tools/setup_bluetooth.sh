#!/bin/bash
# tools/setup_bluetooth.sh  — Linux host / Docker setup

set -e
echo "[*] Setting up Bluetooth research environment (Linux)..."

sudo apt-get update
sudo apt-get install -y \
    bluez \
    bluez-tools \
    bluetooth \
    libbluetooth-dev \
    python3-pip \
    python3-dev \
    wireshark \
    tcpdump

pip3 install -r requirements.txt || true
pip3 install -r requirements-linux.txt || true

sudo systemctl enable bluetooth || true
sudo systemctl start bluetooth || true

sudo hciconfig hci0 up || true

mkdir -p data/captures data/analysis reports logs

echo "[+] Setup complete."
echo "    Verify adapter:   hciconfig -a"
echo "    Scan (bluez):     bluetoothctl -- scan on"
