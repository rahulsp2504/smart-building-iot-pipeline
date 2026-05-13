"""
Occupancy Predictor
====================
Lightweight two-component model suitable for sub-second control loop inference.

Component 1 — Time-of-day baseline
  Queries occupancy_baselines table (hourly averages per zone × weekday).
  Updated on startup and every 30 min from actual readings.
  Captures the structured weekly pattern (9-5 weekday vs weekend).

Component 2 — Exponential smoothing on recent signal
  Exponential weighted average of the last 6 occupancy readings per zone.
  Alpha = 0.4 → recent readings weighted ~40%, decaying backward.
  Captures real-time deviations (late start, surprise meeting, etc.).

Prediction = 0.55 × baseline_for_(hour+N) + 0.45 × smoothed_recent
  N = lookahead_minutes // 60  (rounded to nearest hour bucket)

Output per zone:
  predicted_occupancy  (float)
  confidence           (0–1, based on sample count in baseline)
  occupancy_ratio      (predicted / capacity)
  is_low_occupancy     (ratio < 0.30 → DR candidate)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import AsyncSessionLocal

logger = logging.getLogger(__name__)

ZONE_CAPACITIES = {
    "zone_1": 20,
    "zone_2": 50,
    "zone_3": 15,
    "zone_4": 30,
}

# In-memory cache: {zone_id: {(hour, dow): avg_occupancy}}
_baselines: Dict[str, Dict[tuple, float]] = {z: {} for z in ZONE_CAPACITIES}
_baseline_samples: Dict[str, Dict[tuple, int]] = {z: {} for z in ZONE_CAPACITIES}

# Recent readings cache for EWA: {zone_id: [float, ...]}
_recent: Dict[str, list] = {z: [] for z in ZONE_CAPACITIES}
RECENT_MAX = 6
ALPHA = 0.4   # EWA decay


def _ewa(values: list) -> float:
    """Exponential weighted average, most recent first."""
    if not values:
        return 0.0
    result = values[0]
    for v in values[1:]:
        result = ALPHA * result + (1 - ALPHA) * v
    return result


async def refresh_baselines() -> None:
    """
    Recompute hourly baselines from sensor_readings in TimescaleDB.
    Runs on startup and every 30 min.
    """
    async with AsyncSessionLocal() as session:
        rows = await session.execute(text("""
            SELECT
                zone_id,
                EXTRACT(HOUR FROM timestamp)::int        AS hour_of_day,
                EXTRACT(DOW FROM timestamp)::int         AS day_of_week,
                AVG(value)                               AS avg_occ,
                COUNT(*)                                 AS samples
            FROM sensor_readings
            WHERE sensor_type = 'occupancy'
              AND timestamp >= NOW() - INTERVAL '14 days'
            GROUP BY zone_id, hour_of_day, day_of_week
        """))
        for row in rows.fetchall():
            zid = row.zone_id
            if zid not in _baselines:
                continue
            key = (int(row.hour_of_day), int(row.day_of_week))
            _baselines[zid][key]         = float(row.avg_occ)
            _baseline_samples[zid][key]  = int(row.samples)

        # Also upsert into DB for persistence
        for zid, hours in _baselines.items():
            for (h, d), avg in hours.items():
                samples = _baseline_samples[zid].get((h, d), 0)
                await session.execute(text("""
                    INSERT INTO occupancy_baselines
                        (zone_id, hour_of_day, day_of_week, avg_occupancy, sample_count, updated_at)
                    VALUES (:zone_id, :hour, :dow, :avg, :samples, NOW())
                    ON CONFLICT (zone_id, hour_of_day, day_of_week)
                    DO UPDATE SET
                        avg_occupancy = EXCLUDED.avg_occupancy,
                        sample_count  = EXCLUDED.sample_count,
                        updated_at    = NOW()
                """), {"zone_id": zid, "hour": h, "dow": d, "avg": avg, "samples": samples})
        await session.commit()

    logger.info(f"[Predictor] Baselines refreshed across {len(_baselines)} zones")


def update_recent(zone_id: str, occupancy: float) -> None:
    """Called by MQTT bridge on each occupancy reading."""
    if zone_id not in _recent:
        return
    _recent[zone_id].insert(0, occupancy)
    _recent[zone_id] = _recent[zone_id][:RECENT_MAX]


def predict(zone_id: str, lookahead_minutes: int = 30) -> dict:
    """
    Returns occupancy prediction for zone_id, lookahead_minutes ahead.
    """
    capacity = ZONE_CAPACITIES.get(zone_id, 1)
    now      = datetime.now(timezone.utc)
    target   = now + timedelta(minutes=lookahead_minutes)

    hour = target.hour
    dow  = target.weekday()   # 0=Mon, 6=Sun
    key  = (hour, dow)

    # --- Component 1: time-of-day baseline ---
    baseline    = _baselines.get(zone_id, {}).get(key, None)
    samples     = _baseline_samples.get(zone_id, {}).get(key, 0)

    if baseline is None:
        # No historical data yet — use a simple heuristic
        if 9 <= hour <= 17 and dow < 5:
            baseline = capacity * 0.6
        else:
            baseline = capacity * 0.05
        confidence = 0.3
    else:
        confidence = min(1.0, samples / 20)   # full confidence at 20+ samples

    # --- Component 2: EWA on recent readings ---
    recent_signal = _ewa(_recent.get(zone_id, []))

    # --- Blend ---
    predicted = 0.55 * baseline + 0.45 * recent_signal

    # Clamp to capacity
    predicted = max(0.0, min(float(capacity), predicted))
    ratio     = predicted / capacity

    return {
        "zone_id":             zone_id,
        "lookahead_minutes":   lookahead_minutes,
        "predicted_occupancy": round(predicted, 1),
        "occupancy_ratio":     round(ratio, 3),
        "is_low_occupancy":    ratio < 0.30,
        "confidence":          round(confidence, 2),
        "capacity":            capacity,
        "baseline_used":       round(baseline, 1),
        "recent_signal":       round(recent_signal, 1),
        "target_time":         target.isoformat(),
    }


def predict_all(lookahead_minutes: int = 30) -> dict:
    """Predict occupancy for all zones."""
    return {zid: predict(zid, lookahead_minutes) for zid in ZONE_CAPACITIES}


async def run_periodic_refresh(interval_seconds: int = 1800) -> None:
    """Background task: refresh baselines every 30 min."""
    while True:
        try:
            await refresh_baselines()
        except Exception as exc:
            logger.warning(f"[Predictor] Baseline refresh failed: {exc}")
        await asyncio.sleep(interval_seconds)
