#!/bin/bash
# Hourly energy system monitor — collects system state, asks Claude to
# analyse it, sends the result to Discord.

cd /home/david/HomeAssistant

# Collect all state upfront (no tool use needed by Claude)
STATUS=$(curl -s http://localhost:8214/status 2>/dev/null)
RATE=$(curl -s -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJmYzJkMGMwY2Q2ODg0NmU0YmNkYzY2M2ZmNjk4ODdlYSIsImlhdCI6MTc3MjM2NzgyNiwiZXhwIjoyMDg3NzI3ODI2fQ.XmoHGYnYZ9VO9ww-Z_1lyWHg-IuQBRtX0VqCWKlBfr8" \
    http://192.168.1.83:8123/api/states/sensor.octopus_energy_electricity_23j0257374_1610004326540_current_rate 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])" 2>/dev/null)
TARGET=$(curl -s -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJmYzJkMGMwY2Q2ODg0NmU0YmNkYzY2M2ZmNjk4ODdlYSIsImlhdCI6MTc3MjM2NzgyNiwiZXhwIjoyMDg3NzI3ODI2fQ.XmoHGYnYZ9VO9ww-Z_1lyWHg-IuQBRtX0VqCWKlBfr8" \
    http://192.168.1.83:8123/api/states/input_number.alphaess_target_soc 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])" 2>/dev/null)
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
- Battery power: positive = discharging (good during peak), negative = charging.
- If dispatch is active during peak rate and battery is charging, that is CRITICAL.
- SOC below 20% at any time is concerning.
- Controller connected should be true.

Output ONLY a single Discord message. If normal:
✅ [${NOW}] SOC X% | Battery XkW discharge | Grid XW | Target X% | All normal

If something is wrong, start with 🚨 and explain concisely.

Output NOTHING else." --print 2>/dev/null)

# Send to Discord
if [ -n "$RESULT" ]; then
    curl -s -X POST "REDACTED_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "{\"content\": $(echo "$RESULT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')}" > /dev/null
fi
