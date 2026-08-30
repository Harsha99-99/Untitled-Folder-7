"""Bluetooth Audio Research — source package.

Educational / authorized-testing use only. Test devices you own.
Note: the socket-level Bluetooth modules require Linux + BlueZ; they will
not import/run on Windows (no AF_BLUETOOTH socket family).
"""

__all__ = [
    "scanner",
    "service_enum",
    "audio_capture",
    "hci_monitor",
    "analyzer",
    "poc_framework",
]
