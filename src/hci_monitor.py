#!/usr/bin/env python3
# src/hci_monitor.py
#
# NOTE: HCI monitoring uses Linux BlueZ raw HCI sockets. This observes the
# HCI traffic of your OWN local adapter (the connections it is party to).
# It is not a remote wiretap and does not defeat link encryption.
# Linux only; authorized / own-device testing.

import socket
import struct
import logging
import os
import json
from datetime import datetime


class HCIMonitor:
    """Monitor the local adapter's HCI layer for SCO/eSCO/ACL packets"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.hci_socket = None
        self.monitoring = False
        self.packet_count = 0
        self.audio_packets = []

    def setup_hci_socket(self, device_id=0):
        """Setup raw HCI socket for packet capture (Linux only)"""
        print(f"[*] Setting up HCI monitor on device {device_id}")

        if not hasattr(socket, "AF_BLUETOOTH"):
            self.logger.error("AF_BLUETOOTH unavailable — Linux/BlueZ required")
            print("[-] HCI sockets not supported on this platform")
            return False

        try:
            self.hci_socket = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_RAW,
                socket.BTPROTO_HCI,
            )
            self.hci_socket.bind((device_id,))
            self.set_hci_filter()
            print("[+] HCI monitor ready")
            return True
        except Exception as e:
            self.logger.error(f"HCI setup failed: {e}")
            return False

    def set_hci_filter(self):
        """Set HCI filter to include SCO/eSCO/ACL packet types"""
        # type_mask, then event/opcode masks. Illustrative filter.
        filter_mask = struct.pack(
            "<IIIIII",
            0xFFFFFFFF,  # type_mask - all types
            0x00000002,  # SCO
            0x00000003,  # eSCO
            0x00000004,  # ACL (potential audio)
            0x00000000,
            0x00000000,
        )
        SOL_HCI = getattr(socket, "SOL_HCI", 0)
        HCI_FILTER = getattr(socket, "HCI_FILTER", 2)
        try:
            self.hci_socket.setsockopt(SOL_HCI, HCI_FILTER, filter_mask)
        except Exception as e:
            self.logger.debug(f"Filter set failed (continuing): {e}")

    def start_monitoring(self, duration=60):
        """Start HCI packet monitoring for a fixed duration"""
        if self.hci_socket is None:
            print("[-] No HCI socket — call setup_hci_socket() first")
            return
        print(f"[*] Starting HCI monitoring for {duration} seconds...")
        self.monitoring = True
        start_time = datetime.now()

        while self.monitoring and (datetime.now() - start_time).seconds < duration:
            try:
                packet = self.hci_socket.recv(255)
                if packet:
                    self.process_packet(packet)
            except Exception as e:
                self.logger.error(f"Monitoring error: {e}")
                break

        self.monitoring = False
        print(f"[+] Monitored {self.packet_count} packets")

    def process_packet(self, packet):
        """Dispatch a captured HCI packet by type"""
        if len(packet) < 4:
            return

        self.packet_count += 1
        packet_type = packet[0]
        packet_info = {
            "type": packet_type,
            "timestamp": datetime.now().isoformat(),
            "length": len(packet),
        }

        if packet_type == 0x02:  # SCO
            self.process_sco_packet(packet, packet_info)
        elif packet_type == 0x03:  # eSCO
            self.process_esco_packet(packet, packet_info)
        elif packet_type == 0x04:  # ACL
            self.audio_packets.append(packet_info)

    def process_sco_packet(self, packet, info):
        if len(packet) >= 4:
            connection_handle = struct.unpack("<H", packet[1:3])[0]
            data_length = packet[3]
            audio_data = packet[4 : 4 + data_length]
            info.update(
                {
                    "connection_handle": connection_handle,
                    "data_length": data_length,
                    "packet_class": "SCO",
                    "audio_payload": len(audio_data),
                }
            )
            self.audio_packets.append(info)
            print(f"[SCO] Handle: {connection_handle}, Data: {data_length} bytes")

    def process_esco_packet(self, packet, info):
        if len(packet) >= 4:
            connection_handle = struct.unpack("<H", packet[1:3])[0]
            data_length = packet[3]
            info.update(
                {
                    "connection_handle": connection_handle,
                    "data_length": data_length,
                    "packet_class": "eSCO",
                }
            )
            self.audio_packets.append(info)
            print(f"[eSCO] Handle: {connection_handle}, Data: {data_length} bytes")

    def save_packet_log(self, filename="hci_packets.json"):
        os.makedirs("data/captures", exist_ok=True)
        path = os.path.join("data", "captures", filename)
        log_data = {
            "total_packets": self.packet_count,
            "audio_packets": self.audio_packets,
            "monitoring_duration": len(self.audio_packets),
        }
        with open(path, "w") as f:
            json.dump(log_data, f, indent=2)
        print(f"[+] Packet log saved to {path}")
