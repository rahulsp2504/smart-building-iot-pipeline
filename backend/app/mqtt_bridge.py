"""
MQTT Bridge
============
Subscribes to all building sensor topics, persists to TimescaleDB,
updates the DR engine's zone snapshot, updates the ML predictor's
recent signal, and broadcasts to WebSocket clients.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Set

import paho.mqtt.client as mqtt
from sqlalchemy import text

from .database import AsyncSessionLocal
from .dr.engine import update_zone_snapshot
from .ml.predictor import update_recent

logger = logging.getLogger(__name__)

MQTT_HOST  = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = "building/zone/#"

_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
_ws_callbacks: Set[Callable] = set()

INSERT_SQL = text("""
    INSERT INTO sensor_readings (zone_id, sensor_type, value, unit, timestamp)
    VALUES (:zone_id, :sensor_type, :value, :unit, :timestamp)
""")


def register_ws(cb: Callable):
    _ws_callbacks.add(cb)

def unregister_ws(cb: Callable):
    _ws_callbacks.discard(cb)


# MQTT callbacks (paho thread)
def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info(f"[MQTT Bridge] Connected to {MQTT_HOST}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC, qos=1)
    else:
        logger.error(f"[MQTT Bridge] Connection failed rc={rc}")

def _on_message(client, userdata, msg: mqtt.MQTTMessage):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        parts   = msg.topic.split("/")
        if len(parts) == 4:
            payload["sensor_type"] = parts[3]
        _queue.put_nowait(payload)
    except Exception as exc:
        logger.warning(f"[MQTT Bridge] Bad message: {exc}")

def _on_disconnect(client, userdata, rc, properties=None, reasoncode=None):
    logger.warning(f"[MQTT Bridge] Disconnected rc={rc}")


async def _consume():
    global _ws_callbacks
    while True:
        try:
            p = await _queue.get()

            zone_id     = p.get("zone_id", "unknown")
            sensor_type = p.get("sensor_type", "")
            value       = float(p.get("value", 0))
            unit        = p.get("unit", "")
            ts_str      = p.get("timestamp", datetime.now(timezone.utc).isoformat())
            ts          = datetime.fromisoformat(ts_str)

            # Persist to TimescaleDB
            try:
                async with AsyncSessionLocal() as session:
                    await session.execute(INSERT_SQL, {
                        "zone_id":     zone_id,
                        "sensor_type": sensor_type,
                        "value":       value,
                        "unit":        unit,
                        "timestamp":   ts,
                    })
                    await session.commit()
            except Exception as db_exc:
                logger.error(f"[MQTT Bridge] DB insert failed: {db_exc}")

            # Update live state for DR engine
            update_zone_snapshot(zone_id, sensor_type, value)

            # Feed ML predictor
            if sensor_type == "occupancy":
                update_recent(zone_id, value)

            # Also track cooling_setpoint from simulator payload
            if "cooling_setpoint" in p:
                update_zone_snapshot(zone_id, "cooling_setpoint", float(p["cooling_setpoint"]))

            # Broadcast to WebSocket clients
            out = json.dumps({
                "zone_id":     zone_id,
                "sensor_type": sensor_type,
                "value":       value,
                "unit":        unit,
                "timestamp":   ts_str,
            })
            dead = set()
            for cb in list(_ws_callbacks):
                try:
                    await cb(out)
                except Exception:
                    dead.add(cb)
            _ws_callbacks -= dead

            _queue.task_done()

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception(f"[MQTT Bridge] Consumer error: {exc}")


async def start() -> asyncio.Task:
    client = mqtt.Client(client_id="backend-bridge", protocol=mqtt.MQTTv5)
    client.on_connect    = _on_connect
    client.on_message    = _on_message
    client.on_disconnect = _on_disconnect
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    task = asyncio.get_running_loop().create_task(_consume())
    logger.info("[MQTT Bridge] Started")
    return task
