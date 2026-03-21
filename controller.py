#!/usr/bin/env python3
"""AlphaESS Modbus Controller — local Modbus TCP control for SMILE-G3.

Polls inverter sensors, pushes to HA, exposes HTTP API for charge control.
Uses dispatch mode (registers 0x0880-0x0887) with FC16 writes.
"""

import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
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
    "soc": (0x0102, 1, "uint16", 0.1),           # Battery SOC %
    "battery_power": (0x0126, 1, "int16", 1),     # Battery W (+discharge, -charge)
    "pv1_power": (0x041F, 2, "uint32", 1),        # PV1 W
    "pv2_power": (0x0423, 2, "uint32", 1),        # PV2 W
    "grid_power": (0x0021, 2, "int32", 1),        # Grid W (+import, -export)
    "load_power": (0x040C, 2, "int32", 1),        # Load W
    "charge_energy": (0x0120, 2, "uint32", 0.1),  # Charge kWh lifetime
    "discharge_energy": (0x0122, 2, "uint32", 0.1),  # Discharge kWh lifetime
    "charge_from_grid": (0x0124, 2, "uint32", 0.1),  # Grid->battery kWh lifetime
    "grid_import": (0x0010, 2, "uint32", 0.01),   # Grid import kWh lifetime
    "grid_export": (0x0012, 2, "uint32", 0.01),   # Grid export kWh lifetime (note: might be wrong addr)
    "solar_total": (0x043E, 2, "uint32", 0.1),    # Solar kWh lifetime
    # Dispatch registers
    "dispatch_start": (0x0880, 1, "uint16", 1),
    "dispatch_power": (0x0881, 2, "int32", 1),    # 32000 offset
    "dispatch_mode": (0x0885, 1, "uint16", 1),
    "dispatch_soc": (0x0886, 1, "uint16", 1),     # /0.4 multiplier
    "dispatch_time": (0x0887, 2, "uint32", 1),
}

DISPATCH_POWER_OFFSET = 32000
DISPATCH_SOC_DIVISOR = 0.4
DISPATCH_MODE_SOC_CONTROL = 2


# ---------------------------------------------------------------------------
# Inverter connection
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

    def _write_registers(self, addr: int, values: list[int]) -> bool:
        """Write registers using FC16 (write multiple registers)."""
        if self._client is None:
            return False
        try:
            result = self._client.write_registers(addr, values, device_id=self.slave_id)
            if not result.isError():
                return True
            log.warning("Write 0x%04x failed: %s", addr, result)
            return False
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

    async def poll(self) -> InverterState:
        """Poll all sensors. Runs in executor to avoid blocking."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(None, self._poll_sync)

    def _poll_sync(self) -> InverterState:
        if not self._connect():
            self.state.connected = False
            return self.state

        self.state.connected = True

        # Read power sensors
        for name in ["soc", "battery_power", "pv1_power", "pv2_power",
                      "grid_power", "load_power"]:
            addr, count, dtype, scale = REG[name]
            val = self._read_register(addr, count, dtype, scale)
            if val is not None:
                setattr(self.state, name, val)

        self.state.pv_power = self.state.pv1_power + self.state.pv2_power

        # Read energy counters (less frequently is fine but we do it anyway)
        for name in ["charge_energy", "discharge_energy", "charge_from_grid",
                      "grid_import", "grid_export", "solar_total"]:
            addr, count, dtype, scale = REG[name]
            val = self._read_register(addr, count, dtype, scale)
            if val is not None:
                setattr(self.state, name, val)

        # Read dispatch state
        addr, count, dtype, scale = REG["dispatch_start"]
        val = self._read_register(addr, count, dtype, scale)
        if val is not None:
            self.state.dispatch_active = val == 1

        self.state.last_update = time.time()
        return self.state

    async def start_charge(self, power_w: int, target_soc: int, duration_s: int) -> bool:
        """Start grid charging via dispatch mode."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch_sync, power_w, target_soc, duration_s, True
            )

    async def start_discharge(self, power_w: int, target_soc: int, duration_s: int) -> bool:
        """Start battery discharge via dispatch mode."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch_sync, power_w, target_soc, duration_s, False
            )

    def _dispatch_sync(self, power_w: int, target_soc: int, duration_s: int, charge: bool) -> bool:
        if not self._connect():
            return False

        # Calculate register values
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

        # Write sequence with delays (matching Alpha2MQTT)
        ok = True
        ok = ok and self._write_registers(0x0880, [1])  # Dispatch on
        time.sleep(0.5)
        ok = ok and self._write_registers(0x0881, [power_high, power_low])  # Power
        time.sleep(0.5)
        ok = ok and self._write_registers(0x0887, [duration_high, duration_low])  # Duration
        time.sleep(0.5)
        ok = ok and self._write_registers(0x0886, [soc_reg])  # SOC target
        time.sleep(0.5)
        ok = ok and self._write_registers(0x0885, [DISPATCH_MODE_SOC_CONTROL])  # Mode

        if ok:
            self.state.dispatch_active = True
            self.state.dispatch_charging = charge
            self.state.dispatch_power_w = power_w if charge else -power_w
            self.state.dispatch_soc_target = target_soc
            self.state.dispatch_duration = duration_s
            self.state.dispatch_started = time.time()
            self.state.dispatch_time_remaining = duration_s
            log.info("Dispatch started successfully")
        else:
            log.error("Dispatch failed — some writes failed")

        return ok

    async def hold(self, duration_s: int = 21600) -> bool:
        """Hold battery at current SOC. House runs from grid."""
        current_soc = int(self.state.soc)
        async with self._lock:
            ok = await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch_sync, 5000, current_soc, duration_s, True
            )
            if ok:
                self.state.dispatch_holding = True
            return ok

    async def stop_dispatch(self) -> bool:
        """Stop dispatch mode, return to normal."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(None, self._stop_sync)

    def _stop_sync(self, reason: str = "manual") -> bool:
        if not self._connect():
            return False
        ok = self._write_registers(0x0880, [0])
        if ok:
            self.state.dispatch_active = False
            self.state.dispatch_charging = False
            self.state.dispatch_holding = False
            self.state.dispatch_power_w = 0
            log.info("Dispatch stopped (%s)", reason)
        return ok

    def check_soc_target(self):
        """Auto-stop dispatch if SOC target reached. Called every poll cycle."""
        if not self.state.dispatch_active:
            return

        if self.state.dispatch_holding:
            return

        soc = self.state.soc
        target = self.state.dispatch_soc_target

        if target <= 0:
            return

        # Update time remaining
        if self.state.dispatch_started > 0:
            elapsed = time.time() - self.state.dispatch_started
            self.state.dispatch_time_remaining = max(0, self.state.dispatch_duration - elapsed)

        # Charging: stop when SOC >= target
        if self.state.dispatch_charging and soc >= target:
            log.info("SOC %.1f%% reached target %d%% — stopping dispatch", soc, target)
            self._stop_sync(reason=f"SOC target {target}% reached")

        # Discharging: stop when SOC <= target
        if not self.state.dispatch_charging and soc <= target:
            log.info("SOC %.1f%% reached discharge floor %d%% — stopping dispatch", soc, target)
            self._stop_sync(reason=f"SOC floor {target}% reached")


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

    async def push(self, state: InverterState):
        """Push all sensor states to HA via REST API."""
        import aiohttp

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

        async with aiohttp.ClientSession() as session:
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
                    payload["attributes"]["icon"] = "mdi:battery-charging" if state.dispatch_active else "mdi:battery"

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
            power_w = int(data.get("power_w", 3000))
            target_soc = int(data.get("target_soc", 95))
            duration_s = int(data.get("duration_s", 21600))

            power_w = max(500, min(8000, power_w))
            target_soc = max(10, min(100, target_soc))
            duration_s = max(60, min(86400, duration_s))

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
            power_w = int(data.get("power_w", 3000))
            target_soc = int(data.get("target_soc", 10))
            duration_s = int(data.get("duration_s", 21600))

            power_w = max(500, min(8000, power_w))
            target_soc = max(5, min(100, target_soc))
            duration_s = max(60, min(86400, duration_s))

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
            duration_s = int(data.get("duration_s", 21600))
            duration_s = max(60, min(86400, duration_s))
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
    """Poll inverter and push to HA on a fixed interval."""
    while True:
        try:
            state = await controller.poll()
            if state.connected:
                # Check if dispatch should auto-stop based on SOC
                controller.check_soc_target()
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

    # Start poll loop
    poll_task = asyncio.create_task(
        poll_loop(controller, pusher, inv.get("poll_interval", 5))
    )

    # Start HTTP server
    runner = web.AppRunner(api.app)
    await runner.setup()
    site = web.TCPSite(runner, srv.get("host", "0.0.0.0"), srv.get("port", 8214))
    await site.start()
    log.info("API server listening on %s:%d", srv.get("host", "0.0.0.0"), srv.get("port", 8214))

    # Wait forever
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
