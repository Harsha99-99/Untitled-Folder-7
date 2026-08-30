# Dockerfile — Linux BlueZ environment for the Classic-BT / SCO / HCI modules.
#
# The socket-level Bluetooth code (AF_BLUETOOTH / BTPROTO_SCO / BTPROTO_HCI)
# only exists on Linux. This image gives you that environment on any host.
#
# Requires: Docker with access to the host Bluetooth adapter. On Windows/WSL2
# the host cannot pass a Bluetooth HCI device into the container reliably, so
# the SCO/HCI features need a real Linux host (or a USB BT dongle attached to
# a Linux VM / WSL with usbipd). BLE tests should be run natively on Windows.

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        bluez-tools \
        libbluetooth-dev \
        bluetooth \
        dbus \
        tshark \
        tcpdump \
        gcc \
        pkg-config \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-linux.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-linux.txt

COPY . .

# Default: open a shell so you can drive tools interactively.
CMD ["/bin/bash"]
