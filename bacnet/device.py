"""
Mock BACnet Device
==================
Simulates a real BACnet IP device (like an AHU controller) using bacpypes3.

Each zone gets two BACnet AnalogValue objects:
  - cooling_setpoint  (default 24.0°C) — DR engine raises this to shed load
  - heating_setpoint  (default 20.0°C)

Also exposes a lightweight REST API so the simulator can poll current
setpoints without speaking BACnet itself (realistic: a gateway pattern).

BACnet object model:
  Device ID      : 1001
  Vendor ID      : 999  (private/test)
  Object name    : SmartBuilding-Controller-01
  Object type    : analog-value
  Property       : present-value (writable)

Usage:
  python device.py
  BACNET_HOST=0.0.0.0 BACNET_PORT=47808 python device.py
"""

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | bacnet — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BACnet object model (in-memory, mimics real BACnet device state)
# ---------------------------------------------------------------------------

ZONES = ["zone_1", "zone_2", "zone_3", "zone_4"]

ZONE_NAMES = {
    "zone_1": "Conference Room A",
    "zone_2": "Open Office B",
    "zone_3": "Lab C",
    "zone_4": "Lobby",
}

# BACnet object instance IDs (each zone gets a block of 10)
OBJECT_INSTANCES = {
    "zone_1": {"cooling_setpoint": 1, "heating_setpoint": 2},
    "zone_2": {"cooling_setpoint": 11, "heating_setpoint": 12},
    "zone_3": {"cooling_setpoint": 21, "heating_setpoint": 22},
    "zone_4": {"cooling_setpoint": 31, "heating_setpoint": 32},
}

# Default setpoints (°C)
DEFAULT_COOLING = 24.0
DEFAULT_HEATING = 20.0

# Comfort bounds — device will reject writes outside these
COOLING_MIN = 20.0
COOLING_MAX = 28.0
HEATING_MIN = 16.0
HEATING_MAX = 23.0


@dataclass
class BACnetAnalogValue:
    """
    Mirrors a BACnet AnalogValue object (object-type: analog-value).
    Properties: object-identifier, object-name, present-value, units,
                description, out-of-service.
    """
    object_type: str = "analog-value"
    object_instance: int = 0
    object_name: str = ""
    present_value: float = 0.0
    units: str = "degrees-celsius"         # BACnet engineering units
    description: str = ""
    out_of_service: bool = False
    written_by: str = "init"
    written_at: str = ""

    def to_dict(self) -> dict:
        return {
            "object_type":     self.object_type,
            "object_instance": self.object_instance,
            "object_name":     self.object_name,
            "present_value":   self.present_value,
            "units":           self.units,
            "description":     self.description,
            "out_of_service":  self.out_of_service,
            "written_by":      self.written_by,
            "written_at":      self.written_at,
        }


# Build the device object store
_objects: Dict[str, Dict[str, BACnetAnalogValue]] = {}

def _init_objects():
    ts = datetime.now(timezone.utc).isoformat()
    for zone_id in ZONES:
        _objects[zone_id] = {
            "cooling_setpoint": BACnetAnalogValue(
                object_instance=OBJECT_INSTANCES[zone_id]["cooling_setpoint"],
                object_name=f"{zone_id}-cooling-setpoint",
                present_value=DEFAULT_COOLING,
                description=f"{ZONE_NAMES[zone_id]} cooling setpoint",
                written_by="init",
                written_at=ts,
            ),
            "heating_setpoint": BACnetAnalogValue(
                object_instance=OBJECT_INSTANCES[zone_id]["heating_setpoint"],
                object_name=f"{zone_id}-heating-setpoint",
                present_value=DEFAULT_HEATING,
                description=f"{ZONE_NAMES[zone_id]} heating setpoint",
                written_by="init",
                written_at=ts,
            ),
        }

_init_objects()

# Write history (audit log in-memory, bounded)
_write_history: list = []
MAX_HISTORY = 500


# ---------------------------------------------------------------------------
# FastAPI REST gateway
# (Real BACnet/IP UDP is described in the README; this REST layer is the
#  inter-container interface used by the simulator and backend client)
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Mock BACnet Device — SmartBuilding-Controller-01",
    description=(
        "Simulates a BACnet IP AHU controller. "
        "Exposes BACnet AnalogValue objects (cooling/heating setpoints) "
        "for each zone via a REST gateway. "
        "Device ID: 1001 | Vendor: 999 | Protocol: BACnet/IP (simulated)"
    ),
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class WritePropertyRequest(BaseModel):
    """
    Mirrors a BACnet WriteProperty service request.
    Fields map directly to BACnet PDU parameters.
    """
    object_type:         str   = "analog-value"
    object_instance:     int
    property_identifier: str   = "present-value"
    value:               float
    priority:            int   = 8   # BACnet priority array (1=highest, 16=lowest)
    written_by:          str   = "backend"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status":      "ok",
        "device_id":   1001,
        "device_name": "SmartBuilding-Controller-01",
        "zones":       len(ZONES),
        "objects":     sum(len(v) for v in _objects.values()),
        "protocol":    "BACnet/IP (simulated, REST gateway)",
    }


@app.get("/objects")
def list_all_objects():
    """Return all BACnet objects on this device."""
    result = {}
    for zone_id, props in _objects.items():
        result[zone_id] = {k: v.to_dict() for k, v in props.items()}
    return result


@app.get("/objects/{zone_id}")
def get_zone_objects(zone_id: str):
    """Return all BACnet objects for a specific zone."""
    if zone_id not in _objects:
        raise HTTPException(404, f"Zone '{zone_id}' not found on this device")
    return {k: v.to_dict() for k, v in _objects[zone_id].items()}


@app.get("/objects/{zone_id}/{property_name}")
def read_property(zone_id: str, property_name: str):
    """
    BACnet ReadProperty equivalent.
    Returns the present-value of a specific object.
    """
    if zone_id not in _objects:
        raise HTTPException(404, f"Zone '{zone_id}' not found")
    if property_name not in _objects[zone_id]:
        raise HTTPException(404, f"Property '{property_name}' not found for {zone_id}")
    return _objects[zone_id][property_name].to_dict()


@app.post("/write")
def write_property(req: WritePropertyRequest):
    """
    BACnet WriteProperty equivalent.
    Validates comfort bounds, writes present-value, logs the operation.
    """
    # Resolve zone from object instance
    zone_id = None
    prop_name = None
    for zid, props in OBJECT_INSTANCES.items():
        for pname, inst in props.items():
            if inst == req.object_instance:
                zone_id = zid
                prop_name = pname
                break
        if zone_id:
            break

    if not zone_id:
        raise HTTPException(400, f"No object with instance {req.object_instance} found on device")

    # Comfort bound check
    if prop_name == "cooling_setpoint":
        if not (COOLING_MIN <= req.value <= COOLING_MAX):
            raise HTTPException(
                422,
                f"Cooling setpoint {req.value}°C rejected: must be {COOLING_MIN}–{COOLING_MAX}°C "
                f"(BACnet error: VALUE_OUT_OF_RANGE)"
            )
    elif prop_name == "heating_setpoint":
        if not (HEATING_MIN <= req.value <= HEATING_MAX):
            raise HTTPException(
                422,
                f"Heating setpoint {req.value}°C rejected: must be {HEATING_MIN}–{HEATING_MAX}°C"
            )

    obj = _objects[zone_id][prop_name]
    old_value = obj.present_value
    ts = datetime.now(timezone.utc).isoformat()

    obj.present_value = req.value
    obj.written_by    = req.written_by
    obj.written_at    = ts

    entry = {
        "timestamp":         ts,
        "zone_id":           zone_id,
        "property_name":     prop_name,
        "object_instance":   req.object_instance,
        "value_before":      old_value,
        "value_after":       req.value,
        "priority":          req.priority,
        "written_by":        req.written_by,
        "bacnet_service":    "WriteProperty",
        "property_identifier": req.property_identifier,
    }
    _write_history.append(entry)
    if len(_write_history) > MAX_HISTORY:
        _write_history.pop(0)

    logger.info(
        f"WriteProperty | {zone_id} | {prop_name} | "
        f"{old_value}°C → {req.value}°C | by={req.written_by} | pri={req.priority}"
    )

    return {
        "status":        "ok",
        "bacnet_service": "WriteProperty-ACK",
        "zone_id":       zone_id,
        "property_name": prop_name,
        "value_before":  old_value,
        "value_after":   req.value,
        "timestamp":     ts,
    }


@app.post("/reset")
def reset_all_setpoints():
    """Reset all setpoints to defaults (used after DR event completes)."""
    ts = datetime.now(timezone.utc).isoformat()
    for zone_id in ZONES:
        _objects[zone_id]["cooling_setpoint"].present_value = DEFAULT_COOLING
        _objects[zone_id]["cooling_setpoint"].written_by    = "reset"
        _objects[zone_id]["cooling_setpoint"].written_at    = ts
        _objects[zone_id]["heating_setpoint"].present_value = DEFAULT_HEATING
        _objects[zone_id]["heating_setpoint"].written_by    = "reset"
        _objects[zone_id]["heating_setpoint"].written_at    = ts

    logger.info("All setpoints reset to defaults")
    return {"status": "ok", "message": "All setpoints reset to defaults", "timestamp": ts}


@app.get("/write-history")
def get_write_history(limit: int = 50):
    """Return recent WriteProperty operations (audit log)."""
    return _write_history[-limit:]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.getenv("BACNET_REST_HOST", "0.0.0.0")
    port = int(os.getenv("BACNET_REST_PORT", "8001"))
    logger.info(f"BACnet Device REST gateway starting on {host}:{port}")
    logger.info("Device ID: 1001 | SmartBuilding-Controller-01")
    logger.info(f"Objects: {sum(len(v) for v in _objects.values())} AnalogValue objects across {len(ZONES)} zones")
    uvicorn.run(app, host=host, port=port, log_level="warning")
