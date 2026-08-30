#!/usr/bin/env python3
# src/service_enum.py

import json
import logging
import os
from datetime import datetime

try:
    import bluetooth  # pybluez — Linux/Classic BT only
except Exception:  # pragma: no cover
    bluetooth = None


class ServiceEnumerator:
    """Enumerate Bluetooth services and profiles"""

    def __init__(self):
        self.services = []
        self.audio_services = []
        self.logger = logging.getLogger(__name__)

    def enumerate_rfcomm_services(self, target_addr):
        """Enumerate RFCOMM/SDP services on target (requires pybluez)"""
        print(f"[*] Enumerating services on {target_addr}...")

        if bluetooth is None:
            self.logger.error("pybluez not available (Linux/Classic BT required)")
            print("[-] pybluez unavailable — enumeration skipped on this platform")
            return None

        try:
            services = bluetooth.find_service(address=target_addr)

            for service in services:
                service_info = {
                    "name": service.get("name", "Unknown"),
                    "description": service.get("description", ""),
                    "provider": service.get("provider", ""),
                    "protocol": service.get("protocol", ""),
                    "port": service.get("port", 0),
                    "service_id": service.get("service-id", ""),
                    "service_classes": service.get("service-classes", []),
                    "profiles": service.get("profiles", []),
                    "timestamp": datetime.now().isoformat(),
                }

                self.services.append(service_info)
                print(f"[+] Service: {service_info['name']}")
                print(f"    Protocol: {service_info['protocol']}")
                print(f"    Port: {service_info['port']}")
                print(f"    Service ID: {service_info['service_id']}")

                if self.is_audio_service(service_info):
                    self.audio_services.append(service_info)

            return self.services

        except Exception as e:
            self.logger.error(f"Enumeration failed: {e}")
            return None

    def is_audio_service(self, service_info):
        """Check if a discovered service is audio-related"""
        audio_keywords = [
            "headset", "audio", "hands-free", "hfp",
            "a2dp", "hsp", "voice", "microphone",
            "music", "sound", "speaker",
        ]

        service_text = " ".join(
            [
                str(service_info.get("name", "")).lower(),
                str(service_info.get("description", "")).lower(),
                str(service_info.get("provider", "")).lower(),
            ]
        )

        return any(keyword in service_text for keyword in audio_keywords)

    def identify_audio_profiles(self):
        """Identify specific audio profiles among discovered audio services"""
        profiles = {
            "A2DP": {
                "uuid": "0000110d-0000-1000-8000-00805f9b34fb",
                "description": "Advanced Audio Distribution",
                "channels": ["Audio Sink", "Audio Source"],
            },
            "HFP": {
                "uuid": "0000111e-0000-1000-8000-00805f9b34fb",
                "description": "Hands-Free Profile",
                "channels": ["Audio Gateway", "Hands-Free Unit"],
            },
            "HSP": {
                "uuid": "00001108-0000-1000-8000-00805f9b34fb",
                "description": "Headset Profile",
                "channels": ["Audio Gateway", "Headset"],
            },
        }

        detected_profiles = []
        for service in self.audio_services:
            for profile_name, profile_info in profiles.items():
                if profile_info["uuid"] in str(service.get("service_id", "")):
                    detected_profiles.append(
                        {
                            "profile": profile_name,
                            "info": profile_info,
                            "service": service,
                        }
                    )
        return detected_profiles

    def save_results(self, filename="service_enum.json"):
        os.makedirs("data", exist_ok=True)
        path = os.path.join("data", filename)
        with open(path, "w") as f:
            json.dump(
                {"services": self.services, "audio_services": self.audio_services},
                f,
                indent=2,
            )
        print(f"[+] Results saved to {path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    if len(sys.argv) < 2:
        print("Usage: python service_enum.py <target_mac>")
    else:
        e = ServiceEnumerator()
        e.enumerate_rfcomm_services(sys.argv[1])
        print(e.identify_audio_profiles())
        e.save_results()
