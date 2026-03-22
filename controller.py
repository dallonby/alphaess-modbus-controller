#!/usr/bin/env python3
"""AlphaESS Modbus Controller — local Modbus TCP control for SMILE-G3.

Polls inverter sensors, pushes to HA, exposes HTTP API for charge control.
Uses dispatch mode (registers 0x0880-0x0887) with FC16 writes.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from aiohttp import web
from pymodbus.client import ModbusTcpClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register definitions
# ---------------------------------------------------------------------------

REG = {
    # Read-only sensors
    "soc": (0x0102, 1, "uint16", 0.1),
    "battery_power": (0x0126, 1, "int16", 1),
    "pv1_power": (0x041F, 2, "uint32", 1),
    "pv2_power": (0x0423, 2, "uint32", 1),
    "grid_power": (0x0021, 2, "int32", 1),
    "load_power": (0x040C, 2, "int32", 1),
    "charge_energy": (0x0120, 2, "uint32", 0.1),
    "discharge_energy": (0x0122, 2, "uint32", 0.1),
    "charge_from_grid": (0x0124, 2, "uint32", 0.1),
    "grid_import": (0x0010, 2, "uint32", 0.01),
    "grid_export": (0x0012, 2, "uint32", 0.01),
    "solar_total": (0x043E, 2, "uint32", 0.1),
    # Dispatch registers
    "dispatch_start": (0x0880, 1, "uint16", 1),
    "dispatch_power": (0x0881, 2, "int32", 1),
    "dispatch_mode": (0x0885, 1, "uint16", 1),
    "dispatch_soc": (0x0886, 1, "uint16", 1),
    "dispatch_time": (0x0887, 2, "uint32", 1),
}

DISPATCH_POWER_OFFSET = 32000
DISPATCH_SOC_DIVISOR = 0.4
DISPATCH_MODE_SOC_CONTROL = 2


# ---------------------------------------------------------------------------
# Inverter state
# ---------------------------------------------------------------------------

@dataclass
class InverterState:
    soc: float = 0
    battery_power: float = 0
    pv1_power: float = 0
    pv2_power: float = 0
    pv_power: float = 0
    grid_power: float = 0
    load_power: float = 0
    charge_energy: float = 0
    discharge_energy: float = 0
    charge_from_grid: float = 0
    grid_import: float = 0
    grid_export: float = 0
    solar_total: float = 0
    dispatch_active: bool = False
    dispatch_charging: bool = False
    dispatch_holding: bool = False
    dispatch_power_w: float = 0
    dispatch_soc_target: float = 0
    dispatch_time_remaining: float = 0
    dispatch_started: float = 0
    dispatch_duration: float = 0
    last_update: float = 0
    connected: bool = False


# ---------------------------------------------------------------------------
# Inverter controller
# ---------------------------------------------------------------------------

class InverterController:
    def __init__(self, host: str, port: int, slave_id: int):
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._client: ModbusTcpClient | None = None
        self.state = InverterState()
        self._lock = asyncio.Lock()

    def _connect(self) -> bool:
        if self._client is not None and self._client.connected:
            return True
        try:
            self._client = ModbusTcpClient(self.host, port=self.port, timeout=10)
            if self._client.connect():
                log.info("Modbus connected to %s:%d", self.host, self.port)
                return True
            log.warning("Modbus connect failed")
            self._client = None
            return False
        except Exception as e:
            log.warning("Modbus connect error: %s", e)
            self._client = None
            return False

    def _read_register(self, addr: int, count: int, dtype: str, scale: float) -> float | None:
        if self._client is None:
            return None
        try:
            result = self._client.read_holding_registers(addr, count=count, device_id=self.slave_id)
            if result.isError():
                return None
            if dtype == "uint16":
                return result.registers[0] * scale
            elif dtype == "int16":
                raw = result.registers[0]
                if raw > 32767:
                    raw -= 65536
                return raw * scale
            elif dtype == "uint32":
                return ((result.registers[0] << 16) | result.registers[1]) * scale
            elif dtype == "int32":
                raw = (result.registers[0] << 16) | result.registers[1]
                if raw > 2147483647:
                    raw -= 4294967296
                return raw * scale
        except Exception as e:
            log.warning("Read 0x%04x failed: %s", addr, e)
            self._close()
            return None

    def _write_and_verify(self, addr: int, values: list[int], strict: bool = True) -> bool:
        """Write registers using FC16, then read back to confirm.

        Args:
            strict: If True, readback must match exactly. If False, log a
                    warning on mismatch but still return True (inverter may
                    clamp/round some values like duration).
        """
        if self._client is None:
            return False
        try:
            result = self._client.write_registers(addr, values, device_id=self.slave_id)
            if result.isError():
                log.warning("Write 0x%04x failed: %s", addr, result)
                return False

            # Read back and verify
            time.sleep(0.1)
            readback = self._client.read_holding_registers(addr, count=len(values), device_id=self.slave_id)
            if readback.isError():
                log.warning("Write 0x%04x: write OK but readback failed: %s", addr, readback)
                return not strict

            if list(readback.registers) != values:
                if strict:
                    log.warning("Write 0x%04x: VERIFICATION FAILED — wrote %s, read %s",
                                addr, values, list(readback.registers))
                    return False
                else:
                    log.info("Write 0x%04x: value adjusted by inverter — wrote %s, read %s",
                             addr, values, list(readback.registers))

            return True
        except Exception as e:
            log.warning("Write 0x%04x error: %s", addr, e)
            self._close()
            return False

    def _close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self.state.connected = False

    # --- Poll (runs in executor, holds lock) ---

    async def poll(self) -> InverterState:
        """Poll all sensors and check SOC target. Runs in executor."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(None, self._poll_sync)

    def _poll_sync(self) -> InverterState:
        if not self._connect():
            self.state.connected = False
            return self.state

        self.state.connected = True

        for name in ["soc", "battery_power", "pv1_power", "pv2_power",
                      "grid_power", "load_power"]:
            addr, count, dtype, scale = REG[name]
            val = self._read_register(addr, count, dtype, scale)
            if val is not None:
                setattr(self.state, name, val)

        self.state.pv_power = self.state.pv1_power + self.state.pv2_power

        for name in ["charge_energy", "discharge_energy", "charge_from_grid",
                      "grid_import", "grid_export", "solar_total"]:
            addr, count, dtype, scale = REG[name]
            val = self._read_register(addr, count, dtype, scale)
            if val is not None:
                setattr(self.state, name, val)

        # Read dispatch state from hardware (ground truth)
        addr, count, dtype, scale = REG["dispatch_start"]
        val = self._read_register(addr, count, dtype, scale)
        if val is not None:
            hw_active = val == 1
            if self.state.dispatch_active and not hw_active:
                # Hardware ended dispatch (duration expired)
                log.info("Dispatch ended by hardware (duration expired)")
                self.state.dispatch_active = False
                self.state.dispatch_charging = False
                self.state.dispatch_holding = False
                self.state.dispatch_power_w = 0
            elif not self.state.dispatch_active and hw_active:
                # Something else started dispatch (shouldn't happen)
                log.warning("Dispatch active in hardware but not in controller state")
                self.state.dispatch_active = True

        # Update time remaining
        if self.state.dispatch_active and self.state.dispatch_started > 0:
            elapsed = time.time() - self.state.dispatch_started
            self.state.dispatch_time_remaining = max(0, self.state.dispatch_duration - elapsed)

        # Auto-stop check (inside lock, safe from races)
        self._check_soc_target()

        self.state.last_update = time.time()
        return self.state

    def _check_soc_target(self):
        """Auto-stop dispatch if SOC target reached."""
        if not self.state.dispatch_active or self.state.dispatch_holding:
            return

        soc = self.state.soc
        target = self.state.dispatch_soc_target
        if target <= 0:
            return

        battery = self.state.battery_power

        if self.state.dispatch_charging:
            # Switch to hold when SOC reaches target — keeps battery idle,
            # house runs from grid (important during off-peak cheap rate)
            if soc >= target or (soc >= target - 1 and abs(battery) < 300):
                log.info("SOC %.1f%% reached charge target %d%% — switching to hold (battery=%dW)",
                         soc, target, battery)
                current_soc = int(soc)
                ok = self._dispatch_sync(8000, current_soc, self.state.dispatch_duration, True)
                if ok:
                    self.state.dispatch_holding = True
                    log.info("Switched to hold at %d%%", current_soc)
                else:
                    log.warning("Hold switch failed, stopping dispatch instead")
                    self._stop_sync(reason=f"SOC target {target}% reached, hold failed")

        if not self.state.dispatch_charging:
            if soc <= target or (soc <= target + 1 and abs(battery) < 300):
                log.info("SOC %.1f%% reached discharge floor %d%% — stopping (battery=%dW)",
                         soc, target, battery)
                self._stop_sync(reason=f"SOC floor {target}% reached")

    # --- Dispatch control (all run in executor, all hold lock) ---

    async def start_charge(self, power_w: int, target_soc: int, duration_s: int) -> bool:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch_sync, power_w, target_soc, duration_s, True
            )

    async def start_discharge(self, power_w: int, target_soc: int, duration_s: int) -> bool:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch_sync, power_w, target_soc, duration_s, False
            )

    async def hold(self, duration_s: int = 21600) -> bool:
        """Hold battery at current SOC. House runs from grid."""
        async with self._lock:
            current_soc = int(self.state.soc)
            ok = await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch_sync, 5000, current_soc, duration_s, True
            )
            if ok:
                self.state.dispatch_holding = True
            return ok

    async def stop_dispatch(self) -> bool:
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(None, self._stop_sync)

    def _dispatch_sync(self, power_w: int, target_soc: int, duration_s: int, charge: bool) -> bool:
        if not self._connect():
            return False

        if charge:
            power_reg = DISPATCH_POWER_OFFSET - abs(power_w)
        else:
            power_reg = DISPATCH_POWER_OFFSET + abs(power_w)

        soc_reg = int(target_soc / DISPATCH_SOC_DIVISOR)
        power_high = (power_reg >> 16) & 0xFFFF
        power_low = power_reg & 0xFFFF
        duration_high = (duration_s >> 16) & 0xFFFF
        duration_low = duration_s & 0xFFFF

        log.info("Dispatch: %s %dW to %d%% for %ds (power_reg=%d, soc_reg=%d)",
                 "charge" if charge else "discharge", power_w, target_soc, duration_s,
                 power_reg, soc_reg)

        # Dispatch start FIRST — inverter only accepts config writes when
        # dispatch is active. Matches Alpha2MQTT sequence.
        # (addr, values, name, strict_verify)
        writes = [
            (0x0880, [1], "dispatch_start", True),
            (0x0881, [power_high, power_low], "power", False),  # may read back stale until applied
            (0x0887, [duration_high, duration_low], "duration", False),  # inverter may clamp
            (0x0886, [soc_reg], "soc_target", False),  # may read back stale
            (0x0885, [DISPATCH_MODE_SOC_CONTROL], "mode", False),  # triggers application of above
        ]

        for addr, values, name, strict in writes:
            if not self._write_and_verify(addr, values, strict=strict):
                log.error("Dispatch write %s (0x%04x) failed — rolling back", name, addr)
                self._write_and_verify(0x0880, [0])
                return False
            time.sleep(0.3)

        self.state.dispatch_active = True
        self.state.dispatch_charging = charge
        self.state.dispatch_holding = False
        self.state.dispatch_power_w = power_w if charge else -power_w
        self.state.dispatch_soc_target = target_soc
        self.state.dispatch_duration = duration_s
        self.state.dispatch_started = time.time()
        self.state.dispatch_time_remaining = duration_s
        log.info("Dispatch started (verified)")
        return True

    def _stop_sync(self, reason: str = "manual") -> bool:
        if not self._connect():
            return False
        ok = self._write_and_verify(0x0880, [0])
        if ok:
            self.state.dispatch_active = False
            self.state.dispatch_charging = False
            self.state.dispatch_holding = False
            self.state.dispatch_power_w = 0
            log.info("Dispatch stopped (%s)", reason)
        else:
            log.error("Failed to stop dispatch!")
        return ok


# ---------------------------------------------------------------------------
# HA state pusher
# ---------------------------------------------------------------------------

class HAPusher:
    def __init__(self, ha_url: str, ha_token: str, sensor_map: dict):
        self.ha_url = ha_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        }
        self.sensor_map = sensor_map
        self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def push(self, state: InverterState):
        session = await self._get_session()

        # If disconnected, mark all sensors unavailable so HA alerts fire
        if not state.connected:
            for entity_id in self.sensor_map.values():
                if entity_id is None:
                    continue
                try:
                    url = f"{self.ha_url}/api/states/{entity_id}"
                    payload = {"state": "unavailable", "attributes": {"friendly_name": entity_id.replace("sensor.", "").replace("_", " ").title()}}
                    async with session.post(url, json=payload, headers=self.headers, timeout=5) as resp:
                        pass
                except Exception:
                    pass
            return

        sensors = [
            (self.sensor_map.get("battery_soc"), state.soc, "%", "battery", "measurement"),
            (self.sensor_map.get("battery_power"), state.battery_power, "W", "power", "measurement"),
            (self.sensor_map.get("pv1_power"), state.pv1_power, "W", "power", "measurement"),
            (self.sensor_map.get("pv2_power"), state.pv2_power, "W", "power", "measurement"),
            (self.sensor_map.get("pv_power"), state.pv_power, "W", "power", "measurement"),
            (self.sensor_map.get("grid_power"), state.grid_power, "W", "power", "measurement"),
            (self.sensor_map.get("load_power"), state.load_power, "W", "power", "measurement"),
            (self.sensor_map.get("charge_energy"), state.charge_energy, "kWh", "energy", "total_increasing"),
            (self.sensor_map.get("discharge_energy"), state.discharge_energy, "kWh", "energy", "total_increasing"),
            (self.sensor_map.get("grid_import"), state.grid_import, "kWh", "energy", "total_increasing"),
            (self.sensor_map.get("grid_export"), state.grid_export, "kWh", "energy", "total_increasing"),
            (self.sensor_map.get("solar_total"), state.solar_total, "kWh", "energy", "total_increasing"),
            (self.sensor_map.get("dispatch_active"), "on" if state.dispatch_active else "off", None, None, None),
        ]

        for entity_id, value, uom, device_class, state_class in sensors:
            if entity_id is None:
                continue
            payload = {
                "state": value if isinstance(value, str) else round(value, 2),
                "attributes": {
                    "friendly_name": entity_id.replace("sensor.", "").replace("_", " ").title(),
                },
            }
            if uom:
                payload["attributes"]["unit_of_measurement"] = uom
            if device_class:
                payload["attributes"]["device_class"] = device_class
            if state_class:
                payload["attributes"]["state_class"] = state_class
            if entity_id == self.sensor_map.get("dispatch_active"):
                payload["attributes"]["power_w"] = state.dispatch_power_w
                payload["attributes"]["soc_target"] = state.dispatch_soc_target
                payload["attributes"]["holding"] = state.dispatch_holding
                payload["attributes"]["time_remaining"] = round(state.dispatch_time_remaining)
                payload["attributes"]["icon"] = (
                    "mdi:battery-lock" if state.dispatch_holding
                    else "mdi:battery-charging" if state.dispatch_active
                    else "mdi:battery"
                )

            try:
                url = f"{self.ha_url}/api/states/{entity_id}"
                async with session.post(url, json=payload, headers=self.headers, timeout=5) as resp:
                    if resp.status not in (200, 201):
                        log.warning("Push %s failed: %d", entity_id, resp.status)
            except Exception as e:
                log.warning("Push %s error: %s", entity_id, e)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class APIServer:
    def __init__(self, controller: InverterController):
        self.controller = controller
        self.app = web.Application()
        self.app.router.add_get("/status", self.handle_status)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/charge", self.handle_charge)
        self.app.router.add_post("/discharge", self.handle_discharge)
        self.app.router.add_post("/hold", self.handle_hold)
        self.app.router.add_post("/stop", self.handle_stop)

    async def handle_status(self, request):
        s = self.controller.state
        return web.json_response({
            "soc": s.soc,
            "battery_power": s.battery_power,
            "pv_power": s.pv_power,
            "grid_power": s.grid_power,
            "load_power": s.load_power,
            "dispatch_active": s.dispatch_active,
            "dispatch_charging": s.dispatch_charging,
            "dispatch_holding": s.dispatch_holding,
            "dispatch_power_w": s.dispatch_power_w,
            "dispatch_soc_target": s.dispatch_soc_target,
            "dispatch_time_remaining": s.dispatch_time_remaining,
            "connected": s.connected,
            "last_update": s.last_update,
        })

    async def handle_health(self, request):
        s = self.controller.state
        healthy = s.connected and (time.time() - s.last_update) < 30
        return web.json_response(
            {"healthy": healthy, "connected": s.connected},
            status=200 if healthy else 503,
        )

    async def handle_charge(self, request):
        try:
            data = await request.json()
            power_w = max(500, min(8000, int(data.get("power_w", 3000))))
            target_soc = max(10, min(100, int(data.get("target_soc", 95))))
            duration_s = max(60, min(86400, int(data.get("duration_s", 21600))))

            ok = await self.controller.start_charge(power_w, target_soc, duration_s)
            return web.json_response(
                {"ok": ok, "power_w": power_w, "target_soc": target_soc, "duration_s": duration_s},
                status=200 if ok else 500,
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_discharge(self, request):
        try:
            data = await request.json()
            power_w = max(500, min(8000, int(data.get("power_w", 3000))))
            target_soc = max(5, min(100, int(data.get("target_soc", 10))))
            duration_s = max(60, min(86400, int(data.get("duration_s", 21600))))

            ok = await self.controller.start_discharge(power_w, target_soc, duration_s)
            return web.json_response(
                {"ok": ok, "power_w": power_w, "target_soc": target_soc, "duration_s": duration_s},
                status=200 if ok else 500,
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_hold(self, request):
        try:
            data = await request.json() if request.content_length else {}
            duration_s = max(60, min(86400, int(data.get("duration_s", 21600))))
            ok = await self.controller.hold(duration_s)
            return web.json_response(
                {"ok": ok, "soc": self.controller.state.soc, "duration_s": duration_s},
                status=200 if ok else 500,
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_stop(self, request):
        ok = await self.controller.stop_dispatch()
        return web.json_response({"ok": ok}, status=200 if ok else 500)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def poll_loop(controller: InverterController, pusher: HAPusher, interval: float):
    while True:
        try:
            state = await controller.poll()
            # Always push (even if disconnected — pushes stale=False or last values)
            await pusher.push(state)
        except Exception as e:
            log.error("Poll error: %s", e)
        await asyncio.sleep(interval)


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        config_path = Path(__file__).parent / path
    with open(config_path) as f:
        return yaml.safe_load(f)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    inv = config["inverter"]
    ha = config["homeassistant"]
    srv = config.get("server", {})

    controller = InverterController(inv["host"], inv["port"], inv["slave_id"])
    pusher = HAPusher(ha["url"], ha["token"], config.get("sensors", {}))
    api = APIServer(controller)

    poll_task = asyncio.create_task(
        poll_loop(controller, pusher, inv.get("poll_interval", 5))
    )

    runner = web.AppRunner(api.app)
    await runner.setup()
    site = web.TCPSite(runner, srv.get("host", "0.0.0.0"), srv.get("port", 8214))
    await site.start()
    log.info("API server listening on %s:%d", srv.get("host", "0.0.0.0"), srv.get("port", 8214))

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        poll_task.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
