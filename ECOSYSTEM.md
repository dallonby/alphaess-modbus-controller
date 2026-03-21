# Energy Management Ecosystem

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Local Machine                                 │
│                                                                      │
│  ┌──────────────────────┐     ┌──────────────────────────────────┐  │
│  │  Modbus Controller   │     │  Energy Prediction (ha-energy-   │  │
│  │  (Docker :8214)      │     │  predict/)                       │  │
│  │                      │     │                                  │  │
│  │  • Polls inverter    │     │  • LightGBM load model           │  │
│  │    every 5s          │     │  • Solcast solar forecast         │  │
│  │  • Pushes sensors    │     │  • Calculates optimal SOC target │  │
│  │    to HA via REST    │     │  • Runs every 30min via cron     │  │
│  │  • HTTP API for      │     │  • Trains weekly on Sunday 02:00 │  │
│  │    charge control    │     │  • Validates daily at 06:00      │  │
│  └────────┬─────────────┘     └──────────────┬───────────────────┘  │
│           │ Modbus TCP                        │ REST API             │
│           │ FC16 writes                       │                      │
└───────────┼───────────────────────────────────┼──────────────────────┘
            │                                   │
            ▼                                   ▼
┌───────────────────────┐       ┌───────────────────────────────────┐
│  AlphaESS SMILE-G3    │       │  Home Assistant (192.168.1.83)    │
│  (192.168.1.51:502)   │       │                                   │
│                       │       │  Sensors:                         │
│  • 38.4kWh battery    │       │   sensor.alphaess_battery_soc     │
│  • 9.9kWp solar       │       │   sensor.alphaess_battery_power   │
│  • 8kW inverter       │       │   sensor.alphaess_pv_power        │
│  • Slave ID: 0x55     │       │   sensor.alphaess_grid_power      │
│                       │       │   sensor.predicted_target_soc     │
│  Dispatch registers:  │       │                                   │
│   0x0880 Start        │       │  Automations:                     │
│   0x0881 Power        │       │   Off-Peak Grid Charge            │
│   0x0885 Mode         │       │   Smart Overnight Drain           │
│   0x0886 SOC target   │       │   Overwatch (24/7)                │
│   0x0887 Duration     │       │   Overnight Charge                │
└───────────────────────┘       │                                   │
                                │  rest_commands:                   │
                                │   alphaess_charge → :8214/charge  │
                                │   alphaess_stop   → :8214/stop    │
                                │   discord_notify  → webhook       │
                                └───────────────────────────────────┘
```

## Data Flow

### Every 5 seconds (Modbus Controller)
```
Inverter registers → Modbus TCP read → Controller → REST API POST → HA sensor states
```

Sensors pushed:
| HA Entity | Source Register | Type |
|---|---|---|
| `sensor.alphaess_battery_soc` | 0x0102 (uint16, ×0.1) | Real-time |
| `sensor.alphaess_battery_power` | 0x0126 (int16, W) | Real-time |
| `sensor.alphaess_pv1_power` | 0x041F (uint32, W) | Real-time |
| `sensor.alphaess_pv2_power` | 0x0423 (uint32, W) | Real-time |
| `sensor.alphaess_pv_power` | PV1 + PV2 (template) | Real-time |
| `sensor.alphaess_grid_power` | 0x0021 (int32, W) | Real-time |
| `sensor.alphaess_load_power` | 0x040C (int32, W) | Real-time |
| `sensor.alphaess_battery_charge_energy` | 0x0120 (uint32, ×0.1 kWh) | Lifetime counter |
| `sensor.alphaess_battery_discharge_energy` | 0x0122 (uint32, ×0.1 kWh) | Lifetime counter |
| `sensor.alphaess_grid_import_total` | 0x0010 (uint32, ×0.01 kWh) | Lifetime counter |
| `sensor.alphaess_grid_export_total` | 0x0012 (uint32, ×0.01 kWh) | Lifetime counter |
| `sensor.alphaess_solar_production_total` | 0x043E (uint32, ×0.1 kWh) | Lifetime counter |
| `sensor.alphaess_dispatch_active` | 0x0880 (uint16) | on/off |

### Every 30 minutes (Prediction Pipeline)
```
Octopus rates + Solcast forecast + HA load history + Weather
    → predict.py
    → sensor.predicted_target_soc (pushed to HA)
    → input_number.alphaess_target_soc (set in HA)
    → Discord notification
```

### Charge Control Flow
```
HA automation triggers (rate change / time pattern / SOC change)
    → rest_command.alphaess_charge or alphaess_stop
    → HTTP POST to Controller :8214
    → Controller writes dispatch registers via Modbus FC16
    → Inverter starts/stops charging immediately
```

## Dispatch Mode Protocol

The AlphaESS SMILE-G3 supports a "dispatch mode" where an external controller
temporarily takes over the inverter. This is the ONLY reliable way to control
grid charging via Modbus on the G3.

### To start charging:
```
Register  Value                         Purpose
0x0880    1                             Dispatch on
0x0881    32000 - watts (int32, 2 reg)  Charge power (e.g. 27000 = 5kW)
0x0887    seconds (uint32, 2 regs)      Duration timeout
0x0886    soc_pct / 0.4 (uint16)        Target SOC (e.g. 172 = 69%)
0x0885    2 (uint16)                    Mode: SOC control
```

All writes use **FC16 (Write Multiple Registers)**. FC6 does not work.

### To stop:
```
0x0880    0                             Dispatch off
```

### Important findings:
- Timing registers (0x084F-0x0859) do NOT control charging on the G3
- `charge_cut_soc` register (0x0855) is read-only on the G3 — writes accepted but ignored
- Cloud API and Modbus are independent control planes — invisible to each other
- Dispatch mode is independent of cloud — they don't interfere with each other
- After duration expires, inverter automatically returns to normal mode

### Power encoding:
- 32000 = 0 watts (offset)
- Below 32000 = charging (e.g. 29000 = charge at 3kW)
- Above 32000 = discharging (e.g. 35000 = discharge at 3kW)

### SOC encoding:
- Divide target percentage by 0.4
- e.g. 69% → 69 / 0.4 = 172

## Automations

### Off-Peak Grid Charge (1709294400200)
- **When:** Rate changes or every minute, outside 23:30-05:30
- **Logic:**
  - Cheap rate + SOC below target → `POST /charge`
  - Expensive rate → `POST /stop`
  - Cheap rate + SOC above target → `POST /stop`

### Charge State Overwatch (1709294400201)
- **When:** Every 5 minutes, **24/7** (no time exclusion)
- **Logic:**
  - Battery charging >1kW on peak rate → `POST /stop` + Discord alert
  - Battery charging and SOC >3% above target → `POST /stop` + Discord alert

### Solar-Optimised Overnight Charge (1709294400300)
- **When:** 23:00 daily
- **Logic:**
  - Sets `input_number.alphaess_target_soc` from ML prediction
  - SOC below target → `POST /charge` with 6hr duration
  - SOC already above target → `POST /stop`

### Smart Overnight Battery Drain (1709294400302)
- **When:** Every 1 minute during 23:30-05:30
- **Logic:**
  - SOC above target + smart drain enabled → `POST /stop` (house runs from battery)
  - SOC below target → `POST /charge` to top up
  - SOC at target → `POST /stop` (hold position)
- **Toggle:** `input_boolean.alphaess_smart_battery_drain`

## Safety Layers

Four independent layers prevent overcharging:

| Layer | Mechanism | Frequency | Type |
|---|---|---|---|
| Dispatch SOC target | Register 0x0886 | Continuous | Hardware |
| Dispatch duration | Register 0x0887 | One-shot timer | Hardware |
| Smart Drain | Automation checks SOC vs target | Every 1 min | Software |
| Overwatch | Automation checks power + rate + SOC | Every 5 min, 24/7 | Software |

## Sign Conventions

| Sensor | Positive | Negative |
|---|---|---|
| Battery Power | Discharging (powering house) | Charging (from grid/solar) |
| Grid Power | Importing from grid | Exporting to grid |

## External Dependencies

| Service | What for | Fallback |
|---|---|---|
| Solcast (cloud) | Solar forecast via HA integration | Hardcoded profile in fallback.py |
| Octopus Energy (cloud) | Electricity rates, consumption history | Sensors still in HA from Octopus integration |
| Open-Meteo (cloud) | Historical weather for model training | Cached in weather.parquet |
| Discord (cloud) | Notifications | Non-critical, no fallback needed |

## File Locations

| Component | Path |
|---|---|
| Modbus Controller | `/home/david/alphaess-modbus-controller/` |
| Docker Compose | `/home/david/alphaess-modbus-controller/docker-compose.yaml` |
| Controller Config | `/home/david/alphaess-modbus-controller/config.yaml` |
| Prediction Pipeline | `/home/david/ha-energy-predict/` |
| ML Model | `/home/david/ha-energy-predict/data/model.lgb` |
| Prediction Logs | `/home/david/ha-energy-predict/logs/` |
| HA Config | `/config/configuration.yaml` (on HA VM) |

## Cron Schedule

| Job | When | Command |
|---|---|---|
| Predict | Every 30min | `run.py predict` |
| Train | Sunday 02:00 | `run.py train` |
| Validate | Daily 06:00 | `run.py validate` |

## Solcast Integration

- HACS integration: `solcast_solar` v4.5.0
- Automated dampening **enabled** — learns site-specific shading corrections
- Generation entity: `sensor.alphaess_solar_production_total` (from Modbus controller)
- The prediction pipeline reads `detailedHourly` forecast attributes from Solcast sensors
- Pessimism factor no longer used — Solcast dampening handles correction
