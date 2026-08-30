# Attack Surface Analysis: OnePlus Buds Pro 2

## Bluetooth Interfaces
| Interface | Protocol         | Access Level | Risk   |
|-----------|------------------|--------------|--------|
| HCI       | Raw packets      | System       | High   |
| L2CAP     | Logical channels | Application  | Medium |
| RFCOMM    | Serial emulation | Application  | Medium |
| SCO/eSCO  | Audio channels   | Application  | High   |

## Services
| Service     | UUID   | Profile | Risk   |
|-------------|--------|---------|--------|
| Audio Sink  | 0x110B | A2DP    | Medium |
| Hands-Free  | 0x111E | HFP     | High   |
| Headset     | 0x1108 | HSP     | High   |
| Battery     | 0x180F | BAS     | Low    |
| Device Info | 0x180A | DIS     | Low    |

## Authentication
- Pairing: SSP (Secure Simple Pairing)
- MITM Protection: varies by capability
- Encryption: AES-CCM (when enabled)
- Key Exchange: ECDH (Secure Connections)

## Notes
1. Service discovery is often possible without authentication.
2. Watch for profile/security downgrade behavior.
3. Audio channel access is generally gated on successful pairing.
4. SCO payloads are visible at the HCI layer of a connection you control.
