# Threat Model: Bluetooth Audio Interception

## Assets
- Audio streams (microphone capture)
- Device pairing keys
- User privacy
- Communication content

## Threat Actors
- Eavesdroppers (passive)
- Attackers (active)
- Malicious insiders

## Attack Vectors
1. **Bluetooth Sniffing** — passive capture of BT traffic; requires proximity
   and, for encrypted links, the link key + specialized radio hardware.
2. **MITM Attacks** — intercept/relay during pairing.
3. **Profile Exploitation** — connect to HFP/HSP to reach the mic path.
4. **Firmware Exploitation** — reverse engineer / inject.

## Impact Analysis
- Privacy violation
- Confidential information disclosure
- Personal safety concerns

## Mitigations
- Secure Connections + MITM-protected pairing
- Enforced link encryption for audio
- Access controls
- User awareness
- Regular security updates

## Scope note
This project is scoped to **devices you own** in a controlled environment.
Encrypted modern links cannot be passively decoded without the key and
dedicated hardware; the tooling here observes your own connections.
