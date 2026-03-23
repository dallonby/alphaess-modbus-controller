#!/bin/bash
# Hourly energy system monitor — collects system state, asks Claude to
# analyse it, sends the result to Discord.

export PATH="$HOME/.local/bin:$PATH"
cd /home/david/HomeAssistant

# Read secrets from config.yaml (gitignored)
CONFIG="/home/david/alphaess-modbus-controller/config.yaml"
HA_TOKEN=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG'))['homeassistant']['token'])")
HA_URL=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG'))['homeassistant']['url'])")
DISCORD_WEBHOOK=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG'))['discord']['webhook'])")
CONTROLLER_URL=$(python3 -c "import yaml; c=yaml.safe_load(open('$CONFIG')); print(f'http://localhost:{c[\"server\"][\"port\"]}')")

# Collect all state upfront (no tool use needed by Claude)
STATUS=$(curl -s ${CONTROLLER_URL}/report 2>/dev/null)
RATE=$(curl -s -H "Authorization: Bearer ${HA_TOKEN}" \
    "${HA_URL}/api/states/sensor.octopus_energy_electricity_23j0257374_1610004326540_current_rate" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])" 2>/dev/null)
TARGET=$(curl -s -H "Authorization: Bearer ${HA_TOKEN}" \
    "${HA_URL}/api/states/input_number.alphaess_target_soc" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])" 2>/dev/null)
NOW=$(date '+%H:%M')

# Ask Claude to analyse (no tools needed — all data in the prompt)
RESULT=$(claude -p "You are the energy system monitor. The current time is ${NOW}.

System state:
Controller: ${STATUS}
Electricity rate: ${RATE} GBP/kWh
Target SOC: ${TARGET}%

Rules:
- Off-peak is 23:30-05:30 (rate ~0.07). During off-peak: should be charging toward target or holding at target.
- Peak rate is ~0.298. During peak: battery should discharge to house, grid near 0, no dispatch active unless rate is cheap.
- The controller report above uses unambiguous terms: CHARGING, DISCHARGING, IDLE, HOLDING etc. Trust these descriptions.
- If dispatch is active during peak rate and battery is charging, that is CRITICAL.
- SOC below 20% at any time is concerning.
- Controller connected should be true.

Output ONLY a single Discord message. Keep it dry and concise — one line of stats, maybe a short quip if something interesting is happening. No essays, no flowery language, no monologues.

Normal: ✅ [HH:MM] SOC 61% ↑ | Solar 2kW | Grid 0W | Target 86% — all good
Interesting: ✅ [HH:MM] SOC 95% | Solar 5.2kW | Exporting 1.3kW — panels going hard
Problem: 🚨 [HH:MM] Charging 3kW at 29.8p — something is wrong

One line. Two max if something notable. Output NOTHING else." --print 2>/dev/null)

# Send to Discord
if [ -n "$RESULT" ]; then
    curl -s -X POST "${DISCORD_WEBHOOK}" \
        -H "Content-Type: application/json" \
        -d "{\"content\": $(echo "$RESULT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')}" > /dev/null
fi
