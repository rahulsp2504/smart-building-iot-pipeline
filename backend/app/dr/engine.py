"""
DR Engine
==========
Orchestrates a demand response event end-to-end:

  1. Receive trigger (target_kw, duration_minutes)
  2. Snapshot latest sensor state per zone
  3. Run ML predictor for 30-min occupancy forecast
  4. Run optimizer → shedding plan
  5. Write setpoints via BACnet client
  6. Persist DR event + zone actions to DB
  7. Write audit trail entry
  8. After duration_minutes, restore setpoints + measure actual savings
  9. Update DR event record with actual results
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone


from sqlalchemy import text

from ..database import AsyncSessionLocal
from ..ml import predictor
from ..bacnet_client import write_setpoint, read_setpoint, reset_all_setpoints
from . import optimizer
from . import comfort as comfort_checker

logger = logging.getLogger(__name__)

# In-memory snapshot of latest zone state (updated by MQTT bridge)
_zone_snapshot: dict = {}

# Zone metadata (static)
ZONE_META = {
    "zone_1": {"zone_name": "Conference Room A", "capacity": 20},
    "zone_2": {"zone_name": "Open Office B",     "capacity": 50},
    "zone_3": {"zone_name": "Lab C",             "capacity": 15},
    "zone_4": {"zone_name": "Lobby",             "capacity": 30},
}


def update_zone_snapshot(zone_id: str, sensor_type: str, value: float) -> None:
    """Called by MQTT bridge on each reading. Maintains live state."""
    if zone_id not in _zone_snapshot:
        _zone_snapshot[zone_id] = {}
    _zone_snapshot[zone_id][sensor_type] = value


def get_zone_snapshot() -> dict:
    return dict(_zone_snapshot)


async def _log_audit(
    session,
    event_type: str,
    severity: str,
    message: str,
    zone_id: str = None,
    dr_event_id: str = None,
    metadata: dict = None,
):
    await session.execute(text("""
        INSERT INTO audit_trail (event_type, severity, zone_id, dr_event_id, message, metadata)
        VALUES (:event_type, :severity, :zone_id, :dr_event_id, :message, cast(:metadata as jsonb))
    """), {
        "event_type":  event_type,
        "severity":    severity,
        "zone_id":     zone_id,
        "dr_event_id": dr_event_id,
        "message":     message,
        "metadata":    json.dumps(metadata) if metadata else None,
    })


async def trigger_dr_event(
    target_kw_reduction: float,
    duration_minutes: int,
    triggered_by: str = "api",
    notes: str = None,
) -> dict:
    """
    Full DR event lifecycle. Returns the DR event record.
    The post-event measurement runs as a background task.
    """
    event_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    logger.info(f"[DR] Event {event_id} triggered | target={target_kw_reduction}kW | duration={duration_minutes}min")

    # --- Step 1: Snapshot zone state ---
    zone_states = get_zone_snapshot()
    if not zone_states:
        raise ValueError("No sensor data available — simulator may not be running")

    # --- Step 2: ML predictions ---
    predictions = predictor.predict_all(lookahead_minutes=30)

    # --- Step 3: Compute shedding plan ---
    plan = optimizer.compute(
        target_kw=target_kw_reduction,
        zone_states=zone_states,
        predictions=predictions,
        zone_meta=ZONE_META,
    )

    zones_affected = [a.zone_id for a in plan.zones if a.setpoint_delta_c > 0]

    # --- Step 4: Persist DR event ---
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO dr_events
                (id, triggered_by, status, target_kw_reduction, duration_minutes,
                 zones_affected, started_at, notes)
            VALUES
                (:id, :triggered_by, 'active', :target_kw, :duration,
                 :zones_affected, :started_at, :notes)
        """), {
            "id":             event_id,
            "triggered_by":   triggered_by,
            "target_kw":      target_kw_reduction,
            "duration":       duration_minutes,
            "zones_affected": zones_affected,
            "started_at":     started_at,
            "notes":          notes,
        })

        # Persist zone actions
        for action in plan.zones:
            await session.execute(text("""
                INSERT INTO dr_zone_actions
                    (dr_event_id, zone_id, predicted_occupancy, occupancy_ratio,
                     kw_before, kw_target, setpoint_delta_c, comfort_bound_hit)
                VALUES
                    (:event_id, :zone_id, :pred_occ, :occ_ratio,
                     :kw_before, :kw_target, :delta, :comfort_hit)
            """), {
                "event_id":    event_id,
                "zone_id":     action.zone_id,
                "pred_occ":    action.predicted_occupancy,
                "occ_ratio":   action.occupancy_ratio,
                "kw_before":   action.kw_before,
                "kw_target":   action.kw_projected,
                "delta":       action.setpoint_delta_c,
                "comfort_hit": action.comfort_bound_hit,
            })

        await _log_audit(
            session, "dr_triggered", "info",
            f"DR event {event_id[:8]}… triggered by {triggered_by}. "
            f"Target: {target_kw_reduction} kW | "
            f"Projected shed: {plan.projected_kw_shed:.2f} kW | "
            f"Zones affected: {len(zones_affected)}",
            dr_event_id=event_id,
            metadata={"target_kw": target_kw_reduction, "projected_kw": plan.projected_kw_shed},
        )
        await session.commit()

    # --- Step 5: Issue BACnet writes ---
    kw_before_total = sum(
        zone_states.get(zid, {}).get("energy_kw", 0) for zid in ZONE_META
    )

    async with AsyncSessionLocal() as session:
        for write in plan.setpoint_writes:
            zid = write["zone_id"]
            prop = write["property_name"]
            new_val = write["new_setpoint"]

            old_val = await read_setpoint(zone_id=zid, property_name=prop)

            success, detail = await write_setpoint(
                zone_id=zid,
                property_name=prop,
                new_value=new_val,
                dr_event_id=event_id,
            )
            if success:
                logger.info(f"[DR] Successfully wrote setpoint {new_val} to {zid}. Inserting into setpoint_log.")
                await session.execute(text("""
                    INSERT INTO setpoint_log (zone_id, setpoint_type, value_before, value_after, source, dr_event_id)
                    VALUES (:zone_id, :property_name, :value_before, :value_after, 'dr_engine', :dr_event_id)
                """), {
                    "zone_id": zid,
                    "property_name": prop,
                    "value_before": old_val,
                    "value_after": new_val,
                    "dr_event_id": event_id,
                })
                await session.commit()
            else:
                logger.warning(f"[DR] BACnet write failed for {zid}: {detail}")

    logger.info(f"[DR] {len(plan.setpoint_writes)} setpoints written. Event {event_id[:8]}… active.")

    # --- Step 6: Schedule post-event measurement + restore ---
    asyncio.create_task(
        _post_event_cleanup(event_id, duration_minutes, kw_before_total, len(zones_affected))
    )

    return {
        "event_id":          event_id,
        "status":            "active",
        "target_kw":         target_kw_reduction,
        "projected_kw_shed": plan.projected_kw_shed,
        "target_met":        plan.target_met,
        "duration_minutes":  duration_minutes,
        "zones_affected":    zones_affected,
        "zone_actions": [
            {
                "zone_id":          a.zone_id,
                "zone_name":        a.zone_name,
                "occupancy_ratio":  a.occupancy_ratio,
                "setpoint_delta_c": a.setpoint_delta_c,
                "kw_shed":          a.kw_shed,
                "skip_reason":      a.skip_reason,
            }
            for a in plan.zones
        ],
        "started_at": started_at.isoformat(),
    }


async def _post_event_cleanup(
    event_id: str,
    duration_minutes: int,
    kw_before: float,
    n_zones_affected: int,
):
    """Waits for event duration, measures savings, restores setpoints."""
    await asyncio.sleep(duration_minutes * 60)

    completed_at = datetime.now(timezone.utc)
    zone_states = get_zone_snapshot()

    # Average last 35 seconds of energy readings — more robust than a single snapshot
    async with AsyncSessionLocal() as session:
        rows = await session.execute(text("""
            SELECT AVG(value) as avg_kw
            FROM sensor_readings
            WHERE sensor_type = 'energy_kw'
              AND timestamp >= NOW() - INTERVAL '35 seconds'
        """))
        result = rows.fetchone()
        kw_after_avg = float(result.avg_kw) if result and result.avg_kw else kw_before

    actual_shed = max(0.0, kw_before - kw_after_avg)
    kwh_avoided = round(actual_shed * duration_minutes / 60, 4)

    # Check comfort maintained
    comfort_ok = True
    for zid, state in zone_states.items():
        status = comfort_checker.check(
            zid,
            state.get("temperature", 22.0),
            state.get("co2", 500.0),
            state.get("humidity", 45.0),
        )
        if not status.within_hard:
            comfort_ok = False
            break

    # Restore setpoints
    await reset_all_setpoints()
    logger.info(f"[DR] Event {event_id[:8]}… complete. Shed={actual_shed:.2f}kW | kWh avoided={kwh_avoided}")

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            UPDATE dr_events SET
                status             = 'completed',
                actual_kw_reduction = :actual_shed,
                kwh_avoided        = :kwh_avoided,
                comfort_maintained = :comfort_ok,
                completed_at       = :completed_at
            WHERE id = :event_id
        """), {
            "actual_shed":  round(actual_shed, 3),
            "kwh_avoided":  kwh_avoided,
            "comfort_ok":   comfort_ok,
            "completed_at": completed_at,
            "event_id":     event_id,
        })

        await _log_audit(
            session, "dr_completed", "info",
            f"DR event {event_id[:8]}… completed. "
            f"Actual shed: {actual_shed:.2f} kW | "
            f"kWh avoided: {kwh_avoided} | "
            f"Comfort maintained: {comfort_ok}",
            dr_event_id=event_id,
            metadata={"actual_kw": actual_shed, "kwh_avoided": kwh_avoided, "comfort_ok": comfort_ok},
        )
        await session.commit()
