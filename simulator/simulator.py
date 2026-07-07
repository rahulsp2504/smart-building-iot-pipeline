"""
Smart Building Sensor Simulator
================================
Simulates 4 building zones with realistic physics.

Key differentiator — feedback loop:
  Every tick the simulator polls the BACnet device for current setpoints.
  If the DR engine raised a zone's cooling setpoint, the simulator responds:
    - Temperature drifts toward (setpoint + thermal_lag)
    - energy_kw drops proportionally
  This creates a real closed-loop control system, not just data generation.

Physics model per zone:
  temperature  = f(outdoor_temp, occupancy, cooling_setpoint, thermal_inertia)
  humidity     = f(occupancy, outdoor_humidity)
  co2          = f(occupancy, ventilation_rate)
  occupancy    = probabilistic schedule (business hours)
  energy_kw    = f(occupancy, HVAC_effort, base_load)

MQTT topics:
  building/zone/{zone_id}/temperature  (°C)
  building/zone/{zone_id}/humidity     (%)
  building/zone/{zone_id}/co2          (ppm)
  building/zone/{zone_id}/occupancy    (persons)
  building/zone/{zone_id}/energy_kw    (kW)
"""

import json
import math
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MQTT_HOST          = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT          = int(os.getenv("MQTT_PORT", "1883"))
BACNET_URL         = os.getenv("BACNET_URL", "http://localhost:8001")
PUBLISH_INTERVAL   = float(os.getenv("PUBLISH_INTERVAL", "5"))
SETPOINT_POLL_TICKS = 3   # poll BACnet every N ticks

ZONES = [
    {"id": "zone_1", "name": "Conference Room A", "capacity": 20, "floor": 1,
     "base_kw": 2.5,  "thermal_mass": 0.85},
    {"id": "zone_2", "name": "Open Office B",     "capacity": 50, "floor": 2,
     "base_kw": 6.0,  "thermal_mass": 0.70},
    {"id": "zone_3", "name": "Lab C",             "capacity": 15, "floor": 2,
     "base_kw": 3.5,  "thermal_mass": 0.90},
    {"id": "zone_4", "name": "Lobby",             "capacity": 30, "floor": 1,
     "base_kw": 2.0,  "thermal_mass": 0.60},
]

# Zone state — persists between ticks for thermal inertia
_state = {
    z["id"]: {
        "temperature":      22.0,
        "humidity":         45.0,
        "co2":              450.0,
        "occupancy":        0,
        "energy_kw":        z["base_kw"],
        "cooling_setpoint": 24.0,   # updated from BACnet
        "heating_setpoint": 20.0,
    }
    for z in ZONES
}


# ---------------------------------------------------------------------------
# BACnet setpoint polling
# ---------------------------------------------------------------------------

def _wait_for_bacnet_health():
    """Wait for BACnet REST gateway to become healthy on startup."""
    print("[Simulator] Waiting for BACnet service to be fully ready...")
    while True:
        try:
            resp = requests.get(f"{BACNET_URL}/health", timeout=3)
            if resp.status_code == 200:
                print("[Simulator] BACnet service is ready.")
                break
        except requests.exceptions.RequestException:
            pass
        print("[Simulator] BACnet not ready yet... retrying in 2 seconds.")
        time.sleep(2)


def _fetch_setpoints() -> dict:
    """Poll BACnet device for current setpoints. Returns {zone_id: {cooling, heating}}."""
    try:
        resp = requests.get(f"{BACNET_URL}/objects", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            result = {}
            for zone_id, props in data.items():
                result[zone_id] = {
                    "cooling_setpoint": props.get("cooling_setpoint", {}).get("present_value", 24.0),
                    "heating_setpoint": props.get("heating_setpoint", {}).get("present_value", 20.0),
                }
            return result
    except Exception as exc:
        print(f"[BACnet] Setpoint poll failed: {exc} — using cached values")
    return {}


# ---------------------------------------------------------------------------
# Occupancy schedule
# ---------------------------------------------------------------------------

def _hour_float() -> float:
    now = datetime.now()
    return now.hour + now.minute / 60.0


def _occupancy(hour: float, capacity: int, prev: int) -> int:
    """Business-hours schedule with lunch dip and randomness."""
    if   hour < 7   or hour > 20: prob = 0.03
    elif 7  <= hour < 9:          prob = (hour - 7) / 2 * 0.75
    elif 9  <= hour < 12:         prob = 0.82 + random.gauss(0, 0.04)
    elif 12 <= hour < 13:         prob = 0.35 + random.gauss(0, 0.07)
    elif 13 <= hour < 17:         prob = 0.78 + random.gauss(0, 0.05)
    elif 17 <= hour < 20:         prob = max(0, (20 - hour) / 3 * 0.5)
    else:                         prob = 0.03

    prob = max(0.0, min(1.0, prob))
    target = int(capacity * prob)
    # Gradual drift toward target (not instant jump)
    delta = max(-3, min(3, target - prev + random.randint(-1, 1)))
    return max(0, min(capacity, prev + delta))


# ---------------------------------------------------------------------------
# Physics — each value updates with inertia from previous tick
# ---------------------------------------------------------------------------

def _update_temperature(zone: dict, state: dict) -> float:
    """
    Temperature drifts toward equilibrium driven by:
      - HVAC effort to reach cooling setpoint
      - Occupancy heat load
      - Outdoor temperature influence (sine wave)
      - Thermal mass (inertia coefficient)
    """
    hour = _hour_float()
    outdoor  = 18.0 + 6.0 * math.sin(math.pi * (hour - 6) / 12)  # 18–24°C
    occ_heat = (state["occupancy"] / max(zone["capacity"], 1)) * 3.5

    # HVAC tries to reach cooling setpoint; effort = delta × gain
    setpoint_delta   = state["temperature"] - state["cooling_setpoint"]
    hvac_correction  = -setpoint_delta * 0.3   # proportional controller

    equilibrium = state["cooling_setpoint"] + occ_heat * 0.6 + (outdoor - 22) * 0.15
    drift       = (equilibrium - state["temperature"]) * (1 - zone["thermal_mass"]) * 0.25
    noise       = random.gauss(0, 0.08)

    new_temp = state["temperature"] + drift + noise
    return round(max(16.0, min(32.0, new_temp)), 2)


def _update_humidity(occ: int, capacity: int, prev: float) -> float:
    target = 40.0 + (occ / max(capacity, 1)) * 18.0
    new    = prev + (target - prev) * 0.12 + random.gauss(0, 0.3)
    return round(max(25.0, min(75.0, new)), 2)


def _update_co2(occ: int, capacity: int, prev: float) -> float:
    # 420 ppm baseline; each person adds ~20 ppm; ventilation removes some
    generation  = occ * 20.0
    ventilation = min(generation * 0.6, 200.0)   # HVAC dilution
    target      = 420.0 + generation - ventilation
    new         = prev + (target - prev) * 0.15 + random.gauss(0, 8)
    return round(max(400.0, new), 1)


def _update_energy(zone: dict, state: dict, temp: float) -> float:
    """
    HVAC energy is driven by the gap between outdoor temperature and the
    cooling setpoint — not distance from 22°C.
    Raising the setpoint (DR action) narrows that gap → energy drops.
    """
    hour         = _hour_float()
    outdoor_temp = 18.0 + 6.0 * math.sin(math.pi * (hour - 6) / 12)

    occ_ratio  = state["occupancy"] / max(zone["capacity"], 1)
    lighting   = occ_ratio * 2.5
    equipment  = occ_ratio * 3.5

    cooling_load    = max(0.0, outdoor_temp - state["cooling_setpoint"]) * 1.3
    temp_correction = max(0.0, temp - state["cooling_setpoint"]) * 0.9
    hvac_fan        = 0.8

    noise = random.gauss(0, 0.12)
    total = zone["base_kw"] + lighting + equipment + cooling_load + temp_correction + hvac_fan + noise
    return round(max(0.8, total), 3)


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[MQTT] Connected to {MQTT_HOST}:{MQTT_PORT}")
    else:
        print(f"[MQTT] Connection failed rc={rc}")
        sys.exit(1)


def _on_disconnect(client, userdata, rc, properties=None, reasoncode=None):
    print(f"[MQTT] Disconnected rc={rc}")


def _build_client() -> mqtt.Client:
    client = mqtt.Client(client_id=f"simulator-{os.getpid()}", protocol=mqtt.MQTTv5)
    client.on_connect    = _on_connect
    client.on_disconnect = _on_disconnect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    return client


def _publish_zone(client: mqtt.Client, zone: dict, state: dict, ts: str):
    readings = [
        ("temperature", state["temperature"],  "°C"),
        ("humidity",    state["humidity"],      "%"),
        ("co2",         state["co2"],           "ppm"),
        ("occupancy",   state["occupancy"],     "persons"),
        ("energy_kw",   state["energy_kw"],     "kW"),
    ]
    for sensor_type, value, unit in readings:
        payload = json.dumps({
            "zone_id":            zone["id"],
            "zone_name":          zone["name"],
            "floor":              zone["floor"],
            "sensor_type":        sensor_type,
            "value":              value,
            "unit":               unit,
            "cooling_setpoint":   state["cooling_setpoint"],
            "timestamp":          ts,
        })
        client.publish(f"building/zone/{zone['id']}/{sensor_type}", payload, qos=1)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print(" Smart Building Sensor Simulator")
    print(f" MQTT   : {MQTT_HOST}:{MQTT_PORT}")
    print(f" BACnet : {BACNET_URL}")
    print(f" Zones  : {len(ZONES)} | Interval: {PUBLISH_INTERVAL}s")
    print("=" * 60)

    _wait_for_bacnet_health()

    client = _build_client()
    client.loop_start()
    time.sleep(1.5)   # wait for connect

    running = True
    def _stop(sig, frame):
        nonlocal running
        print("\n[Simulator] Shutting down…")
        running = False
    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    tick = 0
    while running:
        tick += 1
        hour = _hour_float()
        ts   = datetime.now(timezone.utc).isoformat()

        # Poll BACnet for setpoints every N ticks
        if tick % SETPOINT_POLL_TICKS == 0 or tick == 1:
            setpoints = _fetch_setpoints()
            for zone in ZONES:
                zid = zone["id"]
                if zid in setpoints:
                    old_sp = _state[zid]["cooling_setpoint"]
                    new_sp = setpoints[zid]["cooling_setpoint"]
                    if abs(new_sp - old_sp) > 0.01:
                        print(f"[BACnet] {zid} cooling setpoint: {old_sp}°C → {new_sp}°C")
                    _state[zid]["cooling_setpoint"] = new_sp
                    _state[zid]["heating_setpoint"] = setpoints[zid]["heating_setpoint"]

        # Update occupancy every 12 ticks (~1 min)
        if tick % 12 == 0 or tick == 1:
            for zone in ZONES:
                zid = zone["id"]
                _state[zid]["occupancy"] = _occupancy(
                    hour, zone["capacity"], _state[zid]["occupancy"]
                )

        # Update physics and publish
        for zone in ZONES:
            zid = zone["id"]
            st  = _state[zid]

            new_temp     = _update_temperature(zone, st)
            new_humidity = _update_humidity(st["occupancy"], zone["capacity"], st["humidity"])
            new_co2      = _update_co2(st["occupancy"], zone["capacity"], st["co2"])
            new_energy   = _update_energy(zone, st, new_temp)

            _state[zid]["temperature"] = new_temp
            _state[zid]["humidity"]    = new_humidity
            _state[zid]["co2"]         = new_co2
            _state[zid]["energy_kw"]   = new_energy

            _publish_zone(client, zone, _state[zid], ts)

        # Console summary every 6 ticks
        if tick % 6 == 0:
            total_kw = sum(_state[z["id"]]["energy_kw"] for z in ZONES)
            print(
                f"[{ts[:19]}] "
                + " | ".join(
                    f"{z['id']} T={_state[z['id']]['temperature']:.1f}°C "
                    f"occ={_state[z['id']]['occupancy']:2d} "
                    f"SP={_state[z['id']]['cooling_setpoint']:.1f}°C"
                    for z in ZONES
                )
                + f" | total={total_kw:.2f}kW"
            )

        time.sleep(PUBLISH_INTERVAL)

    client.loop_stop()
    client.disconnect()
    print("[Simulator] Stopped.")


if __name__ == "__main__":
    main()
