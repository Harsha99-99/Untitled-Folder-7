#!/usr/bin/env python3
# src/ble_connect.py
#
# Cross-platform (Windows / macOS / Linux) BLE connect + GATT enumeration.
# This is the module you can actually run on Windows to *test functionality*
# against a device you own: scan, connect, and read the GATT service/
# characteristic tree.
#
# Authorized / own-device testing only. Modern earbuds mostly expose their
# audio over Classic Bluetooth (A2DP/HFP), not GATT — so BLE here typically
# surfaces battery/device-info/companion-app services rather than audio.
# Use the Docker/Linux path (hci_monitor.py, audio_capture.py) for the
# Classic-BT audio side.

import asyncio
import argparse
import json
import logging
import os
from datetime import datetime

from bleak import BleakScanner, BleakClient

logger = logging.getLogger(__name__)

# Selected assigned-number descriptions for readability.
KNOWN_SERVICES = {
    "0000180f": "Battery Service",
    "0000180a": "Device Information",
    "00001800": "Generic Access",
    "00001801": "Generic Attribute",
    "0000fd2d": "Vendor / Companion (varies)",
}


def describe_uuid(uuid: str) -> str:
    short = uuid.lower()[:8]
    return KNOWN_SERVICES.get(short, "")


async def scan(timeout: float = 10.0, name_filter: str | None = None):
    print(f"[*] BLE scanning for {timeout:.0f}s ...")
    # bleak >=3.0: return_adv gives {address: (device, advertisement_data)}
    # so we can read RSSI (moved off the device object in bleak 3.x).
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for address, (d, adv) in discovered.items():
        name = d.name or (adv.local_name if adv else None)
        if name_filter and (not name or name_filter.lower() not in name.lower()):
            continue
        rssi = adv.rssi if adv else None
        entry = {"address": address, "name": name, "rssi": rssi}
        results.append(entry)
        print(f"[+] {name or '(no name)':<28} {address}   RSSI={rssi}")
    # Strongest signal first — handy for locating a device you own.
    results.sort(key=lambda e: (e["rssi"] is None, -(e["rssi"] or 0)))
    if not results:
        print("[-] No matching BLE devices found.")
    return results


async def connect_and_enumerate(address: str, save: bool = True):
    print(f"[*] Connecting to {address} ...")
    tree = {"address": address, "timestamp": datetime.now().isoformat(), "services": []}

    async with BleakClient(address) as client:
        connected = client.is_connected
        print(f"[+] Connected: {connected}")

        services = client.services  # already resolved on connect in modern bleak
        for service in services:
            svc = {
                "uuid": str(service.uuid),
                "description": service.description or describe_uuid(str(service.uuid)),
                "characteristics": [],
            }
            print(f"\n[SERVICE] {svc['uuid']}  {svc['description']}")

            for char in service.characteristics:
                props = ",".join(char.properties)
                value_hex = None
                if "read" in char.properties:
                    try:
                        raw = await client.read_gatt_char(char.uuid)
                        value_hex = raw.hex()
                    except Exception as e:
                        value_hex = f"<read failed: {e}>"

                cinfo = {
                    "uuid": str(char.uuid),
                    "description": char.description,
                    "properties": list(char.properties),
                    "value_hex": value_hex,
                }
                svc["characteristics"].append(cinfo)
                print(f"   [CHAR] {char.uuid}  ({props})")
                print(f"          desc: {char.description}")
                if value_hex is not None:
                    print(f"          value: {value_hex}")

            tree["services"].append(svc)

    if save:
        os.makedirs("data", exist_ok=True)
        safe = address.replace(":", "").replace("/", "")
        path = os.path.join("data", f"gatt_{safe}.json")
        with open(path, "w") as f:
            json.dump(tree, f, indent=2)
        print(f"\n[+] GATT tree saved to {path}")
    return tree


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="BLE connect + GATT enumeration (own devices only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan for BLE devices")
    p_scan.add_argument("--timeout", type=float, default=10.0)
    p_scan.add_argument("--name", type=str, default=None, help="Substring name filter")

    p_conn = sub.add_parser("connect", help="Connect to an address and enumerate GATT")
    p_conn.add_argument("address", help="BLE MAC / UUID from the scan")

    args = parser.parse_args()
    if args.cmd == "scan":
        asyncio.run(scan(args.timeout, args.name))
    elif args.cmd == "connect":
        asyncio.run(connect_and_enumerate(args.address))


if __name__ == "__main__":
    main()
