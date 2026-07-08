"""
API Routes
===========
readings.py  — sensor time-series + latest + aggregates
zones.py     — zone metadata + comfort status
dr.py        — trigger DR event, list events, get event detail
audit.py     — audit trail + predictions
"""

# ── readings.py ──────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db

readings_router = APIRouter(prefix="/readings", tags=["readings"])

@readings_router.get("/latest")
async def get_latest(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(text("""
        SELECT zone_id, sensor_type, value, unit, timestamp
        FROM latest_readings ORDER BY zone_id, sensor_type
    """))
    return [dict(r._mapping) for r in rows.fetchall()]


@readings_router.get("/building/summary")
async def building_summary(minutes: int = Query(10, ge=1, le=60), db: AsyncSession = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = await db.execute(text("""
        SELECT sensor_type,
               ROUND(AVG(value)::numeric, 2) AS avg_value,
               ROUND(SUM(CASE WHEN sensor_type='energy_kw' THEN value ELSE 0 END)::numeric, 2) AS total_kw,
               COUNT(*) AS samples
        FROM sensor_readings WHERE timestamp >= :since
        GROUP BY sensor_type ORDER BY sensor_type
    """), {"since": since})
    return {"window_minutes": minutes, "metrics": [dict(r._mapping) for r in rows.fetchall()]}


@readings_router.get("/{zone_id}")
async def zone_readings(
    zone_id: str,
    sensor_type: Optional[str] = None,
    hours: int = Query(1, ge=1, le=168),
    limit: int = Query(300, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    zone_check = await db.execute(text("SELECT zone_id FROM zones WHERE zone_id=:zid"), {"zid": zone_id})
    if not zone_check.fetchone():
        raise HTTPException(404, "Zone not found")

    since  = datetime.now(timezone.utc) - timedelta(hours=hours)
    sql    = "SELECT zone_id, sensor_type, value, unit, timestamp FROM sensor_readings WHERE zone_id=:zid AND timestamp>=:since"
    params = {"zid": zone_id, "since": since, "limit": limit}
    if sensor_type:
        sql += " AND sensor_type=:st"
        params["st"] = sensor_type
    sql += " ORDER BY timestamp DESC LIMIT :limit"
    rows = await db.execute(text(sql), params)
    return [dict(r._mapping) for r in rows.fetchall()]


# ── zones.py ─────────────────────────────────────────────────────────────────

zones_router = APIRouter(prefix="/zones", tags=["zones"])

from ..dr.engine import get_zone_snapshot
from ..dr import comfort as comfort_checker
from ..bacnet_client import get_all_setpoints

@zones_router.get("/")
async def list_zones(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(text(
        "SELECT zone_id, zone_name, floor, capacity, area_sqft FROM zones ORDER BY floor, zone_id"
    ))
    return [dict(r._mapping) for r in rows.fetchall()]


@zones_router.get("/comfort")
async def comfort_status():
    """Live comfort status for all zones — colour-coded for dashboard."""
    snapshot = get_zone_snapshot()
    result   = []
    for zid, state in snapshot.items():
        status = comfort_checker.check(
            zid,
            state.get("temperature", 22.0),
            state.get("co2", 500.0),
            state.get("humidity", 45.0),
        )
        result.append({
            "zone_id":       zid,
            "within_hard":   status.within_hard,
            "within_soft":   status.within_soft,
            "violations":    status.violations,
            "warnings":      status.warnings,
            "current_temp":  status.current_temp,
            "current_co2":   status.current_co2,
            "current_humidity": status.current_humidity,
        })
    return result


@zones_router.get("/setpoints")
async def current_setpoints():
    """Current BACnet setpoints for all zones."""
    return await get_all_setpoints()


# ── dr.py ────────────────────────────────────────────────────────────────────

dr_router = APIRouter(prefix="/dr", tags=["demand-response"])

from pydantic import BaseModel, Field

class DREventRequest(BaseModel):
    target_kw_reduction: float  = Field(..., gt=0, le=100, description="kW to shed")
    duration_minutes:    int    = Field(..., ge=5, le=120)
    triggered_by:        str    = Field("api", description="api | utility_webhook | test")
    notes:               Optional[str] = None

from ..dr.engine import trigger_dr_event
from ..ml.predictor import predict_all

@dr_router.post("/event")
async def create_dr_event(req: DREventRequest):
    """Trigger a demand response event."""
    try:
        result = await trigger_dr_event(
            target_kw_reduction=req.target_kw_reduction,
            duration_minutes=req.duration_minutes,
            triggered_by=req.triggered_by,
            notes=req.notes,
        )
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"DR engine error: {exc}")


@dr_router.get("/events")
async def list_dr_events(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(text("""
        SELECT id, triggered_by, status, target_kw_reduction, actual_kw_reduction,
               kwh_avoided, duration_minutes, comfort_maintained,
               zones_affected, started_at, completed_at, created_at, notes
        FROM dr_events ORDER BY created_at DESC LIMIT :limit
    """), {"limit": limit})
    return [dict(r._mapping) for r in rows.fetchall()]


@dr_router.get("/events/{event_id}")
async def get_dr_event(event_id: str, db: AsyncSession = Depends(get_db)):
    row = await db.execute(text(
        "SELECT * FROM dr_events WHERE id=:id"
    ), {"id": event_id})
    event = row.fetchone()
    if not event:
        raise HTTPException(404, "DR event not found")

    actions = await db.execute(text(
        "SELECT * FROM dr_zone_actions WHERE dr_event_id=:id ORDER BY zone_id"
    ), {"id": event_id})

    return {
        "event":   dict(event._mapping),
        "actions": [dict(a._mapping) for a in actions.fetchall()],
    }


@dr_router.get("/predict")
async def get_predictions(lookahead_minutes: int = Query(30, ge=5, le=60)):
    """ML occupancy predictions for all zones."""
    return predict_all(lookahead_minutes=lookahead_minutes)


# ── audit.py ─────────────────────────────────────────────────────────────────

audit_router = APIRouter(prefix="/audit", tags=["audit"])

@audit_router.get("/")
async def get_audit(
    limit: int = Query(50, ge=1, le=500),
    severity: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    sql    = "SELECT * FROM audit_trail"
    params = {"limit": limit}
    if severity:
        sql += " WHERE severity=:severity"
        params["severity"] = severity
    sql += " ORDER BY timestamp DESC LIMIT :limit"
    rows = await db.execute(text(sql), params)
    return [dict(r._mapping) for r in rows.fetchall()]


@audit_router.get("/setpoints")
async def get_setpoint_log(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(text("""
        SELECT * FROM setpoint_log ORDER BY timestamp DESC LIMIT :limit
    """), {"limit": limit})
    return [dict(r._mapping) for r in rows.fetchall()]
