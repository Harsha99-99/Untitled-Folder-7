#!/usr/bin/env python3
# main.py

import asyncio
import logging
import json
import os
import sys
from datetime import datetime

import yaml

sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from scanner import BluetoothScanner
from service_enum import ServiceEnumerator
from audio_capture import AudioStreamAnalyzer
from hci_monitor import HCIMonitor
from analyzer import AudioAnalyzer
from poc_framework import AudioInterceptionPoC


class BluetoothResearchProject:
    """Main research project orchestrator"""

    def __init__(self, config_file="config/settings.yaml"):
        self.config = self.load_config(config_file)
        self.setup_logging()
        self.logger = logging.getLogger(__name__)

        self.scanner = BluetoothScanner()
        self.service_enum = ServiceEnumerator()
        self.audio_analyzer = AudioStreamAnalyzer()
        self.hci_monitor = HCIMonitor()
        self.poc = AudioInterceptionPoC()
        self.analyzer = AudioAnalyzer()

        self.research_data = {
            "start_time": datetime.now().isoformat(),
            "target_device": None,
            "discovered_devices": [],
            "services": [],
            "audio_profiles": [],
            "capture_sessions": [],
            "analysis_results": [],
            "findings": [],
        }

    def load_config(self, config_file):
        with open(config_file, "r") as f:
            return yaml.safe_load(f)

    def setup_logging(self):
        log_config = self.config.get("logging", {})
        os.makedirs("logs", exist_ok=True)
        logging.basicConfig(
            level=getattr(logging, log_config.get("level", "INFO")),
            format=log_config.get(
                "format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            ),
            handlers=[
                logging.FileHandler(log_config.get("file", "logs/research.log")),
                logging.StreamHandler(),
            ],
        )

    async def run_research(self):
        print("=" * 60)
        print("Bluetooth Audio Security Research Project")
        print("Target: OnePlus Buds Pro 2 (own-device / authorized testing)")
        print("=" * 60)

        print("\n[PHASE 1] Device Discovery")
        print("-" * 40)
        devices = self.scanner.scan_classic_bluetooth()
        if devices:
            self.research_data["discovered_devices"] = devices

        print("\n[PHASE 2] Service Enumeration")
        print("-" * 40)
        if self.scanner.target_device:
            services = self.service_enum.enumerate_rfcomm_services(
                self.scanner.target_device["address"]
            )
            if services:
                self.research_data["services"] = services
                self.research_data["audio_profiles"] = (
                    self.service_enum.identify_audio_profiles()
                )

        print("\n[PHASE 3] Methodology / Flow")
        print("-" * 40)
        self.research_data["attack_flow"] = self.poc.demonstrate_attack_flow()

        print("\n[PHASE 4] Security Assessment")
        print("-" * 40)
        target = (
            self.scanner.target_device.get("address")
            if self.scanner.target_device
            else None
        )
        self.research_data["assessment"] = self.poc.run_automated_assessment(target)

        self.generate_research_report()

    def generate_research_report(self):
        os.makedirs("reports", exist_ok=True)
        report = {
            "project": "Bluetooth Audio Interception Research",
            "target": "OnePlus Buds Pro 2",
            "date": datetime.now().isoformat(),
            "methodology": "Security vulnerability assessment (authorized)",
            "findings": self.research_data["findings"],
            "recommendations": self.generate_recommendations(),
        }
        with open("reports/final_report.json", "w") as f:
            json.dump(report, f, indent=2)
        self.generate_markdown_report(report)

    def generate_recommendations(self):
        return [
            {"category": "Pairing Security", "recommendation": "Use Secure Connections / MITM-protected pairing", "priority": "High"},
            {"category": "Audio Encryption", "recommendation": "Ensure link encryption is enforced for audio", "priority": "Critical"},
            {"category": "Access Control", "recommendation": "Profile-level access controls", "priority": "High"},
            {"category": "Monitoring", "recommendation": "Connection monitoring and alerts", "priority": "Medium"},
        ]

    def generate_markdown_report(self, report):
        md = f"""# Bluetooth Audio Security Research Report

## Project Information
- **Target:** {report['target']}
- **Date:** {report['date']}
- **Methodology:** {report['methodology']}

## Executive Summary
Authorized, own-device assessment of the Bluetooth audio stack for the
OnePlus Buds Pro 2, focused on understanding the attack surface and
defensive posture.

## Findings

### Attack Surface
- Multiple audio profiles (HFP, A2DP, HSP)
- RFCOMM channels for audio control
- SCO/eSCO synchronous audio channels

### Vulnerability Classes
1. Pairing weaknesses (Just Works / downgrade)
2. Unauthenticated service discovery
3. Post-pairing audio channel access
4. HCI-layer observation of your own connection

## Recommendations
{self.format_recommendations(report['recommendations'])}

## Conclusion
Modern devices implement encryption and Secure Connections; this project
documents where configuration and awareness matter most.

---
*Educational and authorized-testing purposes only.*
"""
        with open("reports/final_report.md", "w") as f:
            f.write(md)
        print("[+] Markdown report generated: reports/final_report.md")

    def format_recommendations(self, recommendations):
        out = ""
        for rec in recommendations:
            out += f"### {rec['category']}\n"
            out += f"- **Recommendation:** {rec['recommendation']}\n"
            out += f"- **Priority:** {rec['priority']}\n\n"
        return out


async def main():
    project = BluetoothResearchProject()
    await project.run_research()


if __name__ == "__main__":
    asyncio.run(main())
