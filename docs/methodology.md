# Methodology

Authorized, own-device assessment in a controlled RF environment.

## Phases
1. **Discovery** — enumerate reachable devices (Classic via BlueZ; BLE via bleak).
2. **Service enumeration** — SDP/GATT to identify profiles (HFP/A2DP/HSP; BAS/DIS).
3. **Connection** — pair/connect to your own device's audio profile.
4. **Observation** — capture your own connection's HCI/SCO traffic (btmon / hci_monitor.py).
5. **Analysis** — offline signal analysis of captured audio (analyzer.py).
6. **Reporting** — findings + defensive recommendations.

## Environment split
- **Windows-native:** BLE discovery/connect + all signal analysis.
- **Linux / Docker:** Classic BT, RFCOMM/SDP enumeration, SCO/HCI capture.

## Evidence handling
- Store captures under `data/captures/`.
- Keep a log of every session (time, device, adapter, purpose).
- Delete sensitive captures when analysis is complete.

## Success criteria
Each phase has an explicit success criterion (see `poc_framework.py`
`demonstrate_attack_flow()`), recorded in the final report.
