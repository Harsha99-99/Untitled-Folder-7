#!/usr/bin/env python3
# src/scanner.py

import asyncio
import logging
import json
import os
from datetime import datetime

try:
    import bluetooth  # pybluez — Linux/Classic BT only
except Exception:  # pragma: no cover - platform dependent
    bluetooth = None

try:
    from bleak import BleakScanner, BleakClient  # cross-platform BLE
except Exception:  # pragma: no cover
    BleakScanner = None
    BleakClient = None


class BluetoothScanner:
    """Bluetooth device discovery and reconnaissance"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.discovered_devices = []
        self.target_device = None

    def scan_classic_bluetooth(self, duration=10):
        """Scan for Classic Bluetooth devices (requires pybluez + Linux)"""
        print("[*] Scanning for Classic Bluetooth devices...")

        if bluetooth is None:
            self.logger.error("pybluez not available (Linux/Classic BT required)")
            print("[-] pybluez unavailable — Classic BT scan skipped on this platform")
            return None

        try:
            devices = bluetooth.discover_devices(
                duration=duration,
                lookup_names=True,
                flush_cache=True,
                lookup_class=True,
                device_id=-1,
            )

            for addr, name, device_class in devices:
                device_info = {
                    "address": addr,
                    "name": name,
                    "class": self.decode_device_class(device_class),
                    "timestamp": datetime.now().isoformat(),
                }

                self.discovered_devices.append(device_info)
                print(f"[+] Found: {name} - {addr}")
                print(f"    Class: {device_info['class']}")

                if name and ("OnePlus" in name or "Buds" in name):
                    self.target_device = device_info

            return self.discovered_devices

        except Exception as e:
            self.logger.error(f"Scan failed: {e}")
            return None

    def decode_device_class(self, device_class):
        """Decode Bluetooth device major class"""
        major_classes = {
            0x0001: "Computer",
            0x0002: "Phone",
            0x0004: "Audio/Video",
            0x0005: "Peripheral",
            0x0006: "Imaging",
            0x0007: "Wearable",
            0x0008: "Toy",
            0x0009: "Health",
        }

        major_class = (device_class >> 8) & 0x1F
        return major_classes.get(major_class, f"Unknown (0x{major_class:04x})")

    async def scan_ble_devices(self):
        """Scan for BLE devices (cross-platform via bleak)"""
        print("[*] Scanning for BLE devices...")

        if BleakScanner is None:
            self.logger.error("bleak not available — install with: pip install bleak")
            print("[-] bleak unavailable — BLE scan skipped")
            return None

        devices = await BleakScanner.discover()
        for device in devices:
            if device.name:
                rssi = getattr(device, "rssi", None)
                print(f"[+] BLE Device: {device.name} - {device.address}")
                print(f"    RSSI: {rssi} dBm")

                if "OnePlus" in device.name or "Buds" in device.name:
                    self.target_device = {
                        "address": device.address,
                        "name": device.name,
                        "rssi": rssi,
                    }
        return devices

    def save_results(self, filename="discovery_results.json"):
        """Save discovery results to file"""
        os.makedirs("data", exist_ok=True)
        path = os.path.join("data", filename)
        with open(path, "w") as f:
            json.dump(self.discovered_devices, f, indent=2)
        print(f"[+] Results saved to {path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = BluetoothScanner()
    s.scan_classic_bluetooth()
    try:
        asyncio.run(s.scan_ble_devices())
    except Exception as e:
        print(f"[-] BLE scan error: {e}")
    s.save_results()
