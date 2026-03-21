# AlphaESS Modbus Controller

Local Modbus TCP controller for AlphaESS SMILE-G3 inverters. Direct register control via dispatch mode — no cloud dependency.

## What it does

- Polls inverter sensors every 5 seconds via Modbus TCP
- Pushes real-time state to Home Assistant via REST API
- Controls grid charging/discharging via Modbus dispatch mode (FC16)
- Exposes a simple HTTP API for automations to call
- No AlphaESS cloud API required

## Why

The AlphaESS cloud API is unreliable for real-time battery control:
- 10-30 second propagation delay
- Silent failures (commands accepted but not applied)
- Cloud state and Modbus state are independent — they fight each other
- The `chargestopsoc` parameter doesn't actually control the charge stop SOC on the G3

This controller talks directly to the inverter over your LAN via Modbus TCP, using the dispatch register protocol (0x0880-0x0887) with FC16 writes.

## Hardware

- AlphaESS SMILE-G3 (tested on G3-S5/B5, likely works on other G3 variants)
- Inverter connected to LAN via ethernet (Modbus TCP on port 502)
- Any machine on the same network running Python 3.11+

## Quick Start

```bash
pip install pymodbus aiohttp requests
cp config.example.yaml config.yaml
# Edit config.yaml with your inverter IP and HA details
python controller.py
```

## API

### GET /status
Current inverter state (SOC, power, grid, solar, dispatch status).

### POST /charge
Start grid charging.
```json
{"power_w": 3000, "target_soc": 69, "duration_s": 3600}
```

### POST /discharge
Start battery discharge.
```json
{"power_w": 3000, "target_soc": 30, "duration_s": 3600}
```

### POST /stop
Stop dispatch mode, return to normal operation.

### GET /health
Health check — returns Modbus connection status.

## Home Assistant Integration

Add to `configuration.yaml`:
```yaml
rest_command:
  alphaess_charge:
    url: "http://localhost:8214/charge"
    method: POST
    content_type: "application/json"
    payload: '{"power_w": {{ power }}, "target_soc": {{ soc }}, "duration_s": {{ duration }}}'

  alphaess_stop:
    url: "http://localhost:8214/stop"
    method: POST

  alphaess_discharge:
    url: "http://localhost:8214/discharge"
    method: POST
    content_type: "application/json"
    payload: '{"power_w": {{ power }}, "target_soc": {{ soc }}, "duration_s": {{ duration }}}'
```

Then in automations:
```yaml
action:
  - action: rest_command.alphaess_charge
    data:
      power: 5000
      soc: 69
      duration: 21600
```

## Register Map (SMILE-G3)

| Address | Hex | Name | Type | Notes |
|---|---|---|---|---|
| 258 | 0x0102 | Battery SOC | uint16 | Scale 0.1, read-only |
| 294 | 0x0126 | Battery Power | int16 | Watts, +ve=discharge, -ve=charge |
| 1055 | 0x041F | PV1 Power | uint32 | Watts |
| 1059 | 0x0423 | PV2 Power | uint32 | Watts |
| 33 | 0x0021 | Grid Power | int32 | Watts, +ve=import, -ve=export |
| 1036 | 0x040C | Load Power | int32 | Watts |
| 288 | 0x0120 | Battery Charge Energy | uint32 | Scale 0.1, kWh lifetime |
| 290 | 0x0122 | Battery Discharge Energy | uint32 | Scale 0.1, kWh lifetime |
| 2176 | 0x0880 | Dispatch Start | uint16 | 1=on, 0=off |
| 2177 | 0x0881 | Dispatch Active Power | int32 | 32000 offset. <32000=charge |
| 2181 | 0x0885 | Dispatch Mode | uint16 | 2=SOC control |
| 2182 | 0x0886 | Dispatch SOC | uint16 | Target %, divide by 0.4 |
| 2183 | 0x0887 | Dispatch Time | uint32 | Duration in seconds |

## Acknowledgements

- [Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT) — dispatch register protocol reference
- [AlphaMon](https://alphamon.net/registers/) — register map CSV
- [Gaspode69](https://github.com/Gaspode69/modbus-templates) — register template reference

## License

MIT
