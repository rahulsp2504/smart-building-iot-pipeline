"""
BACnet Client
=============
Issues BACnet WriteProperty and ReadProperty commands to the mock
BACnet device. Uses the device's REST gateway for inter-container
communication (mirrors a BACnet/IP gateway pattern used in real deployments).

BACnet object instance map (matches device.py):
  zone_1: cooling=1, heating=2
  zone_2: cooling=11, heating=12
  zone_3: cooling=21, heating=22
  zone_4: cooling=31, heating=32
"""

import logging
import os
from typing import Tuple

import httpx

logger = logging.getLogger(__name__)

BACNET_URL = os.getenv("BACNET_URL", "http://localhost:8001")

OBJECT_INSTANCES = {
    "zone_1": {"cooling_setpoint": 1,  "heating_setpoint": 2},
    "zone_2": {"cooling_setpoint": 11, "heating_setpoint": 12},
    "zone_3": {"cooling_setpoint": 21, "heating_setpoint": 22},
    "zone_4": {"cooling_setpoint": 31, "heating_setpoint": 32},
}

_client = httpx.AsyncClient(timeout=5.0, base_url=BACNET_URL)


async def write_setpoint(
    zone_id: str,
    property_name: str,
    new_value: float,
    dr_event_id: str = None,
    priority: int = 8,
) -> Tuple[bool, str]:
    """
    Issues a BACnet WriteProperty to set a zone's cooling or heating setpoint.
    Returns (success: bool, detail: str).
    """
    instance = OBJECT_INSTANCES.get(zone_id, {}).get(property_name)
    if instance is None:
        return False, f"Unknown zone/property: {zone_id}/{property_name}"

    payload = {
        "object_type":          "analog-value",
        "object_instance":      instance,
        "property_identifier":  "present-value",
        "value":                new_value,
        "priority":             priority,
        "written_by":           f"dr_engine:{dr_event_id[:8] if dr_event_id else 'manual'}",
    }

    try:
        resp = await _client.post("/write", json=payload)
        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                f"[BACnet] WriteProperty OK | {zone_id} | {property_name} | "
                f"{data.get('value_before')}°C → {new_value}°C"
            )
            return True, resp.text
        else:
            detail = resp.json().get("detail", resp.text)
            logger.warning(f"[BACnet] WriteProperty rejected | {zone_id} | {detail}")
            return False, detail
    except Exception as exc:
        logger.error(f"[BACnet] WriteProperty error: {exc}")
        return False, str(exc)


async def read_setpoint(zone_id: str, property_name: str) -> float:
    """BACnet ReadProperty — returns current present-value."""
    try:
        resp = await _client.get(f"/objects/{zone_id}/{property_name}")
        if resp.status_code == 200:
            return float(resp.json().get("present_value", 24.0))
    except Exception as exc:
        logger.warning(f"[BACnet] ReadProperty error: {exc}")
    return 24.0


async def reset_all_setpoints() -> bool:
    """POST /reset on BACnet device — restores all setpoints to defaults."""
    try:
        resp = await _client.post("/reset")
        if resp.status_code == 200:
            logger.info("[BACnet] All setpoints reset to defaults")
            return True
    except Exception as exc:
        logger.error(f"[BACnet] Reset failed: {exc}")
    return False


async def get_all_setpoints() -> dict:
    """Read all current setpoints from BACnet device."""
    try:
        resp = await _client.get("/objects")
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.warning(f"[BACnet] Get all setpoints failed: {exc}")
    return {}
