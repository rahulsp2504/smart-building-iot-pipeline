"""
Smart Building DR Middleware — Backend API Tests
================================================
Covers the 7 core validation scenarios from the test suite.
Runs against a live stack (TimescaleDB + MQTT + BACnet all up).
Used by GitHub Actions CI with real service containers.

Run locally:
    docker compose up -d
    cd backend
    pip install -r requirements.txt
    pytest tests/ -v
"""

import pytest
import pytest_asyncio
import httpx
import asyncio
import os

API_BASE   = os.getenv("API_BASE",   "http://localhost:8000")
BACNET_BASE = os.getenv("BACNET_BASE", "http://localhost:8001")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def api():
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15.0) as client:
        yield client


@pytest_asyncio.fixture()
async def bacnet():
    async with httpx.AsyncClient(base_url=BACNET_BASE, timeout=10.0) as client:
        yield client


# ---------------------------------------------------------------------------
# Scenario 1 — Pipeline health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(api):
    """All services connected and API responsive."""
    r = await api.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_latest_readings_all_zones(api):
    """20 readings returned — 4 zones × 5 sensor types."""
    r = await api.get("/readings/latest")
    assert r.status_code == 200
    data = r.json()
    # At least some readings present (simulator may not have all 20 if just started)
    assert len(data) > 0

    zone_ids = {d["zone_id"] for d in data}
    sensor_types = {d["sensor_type"] for d in data}

    assert "zone_1" in zone_ids
    assert "zone_2" in zone_ids
    assert "zone_3" in zone_ids
    assert "zone_4" in zone_ids

    expected_sensors = {"temperature", "humidity", "co2", "occupancy", "energy_kw"}
    assert expected_sensors.issubset(sensor_types)


@pytest.mark.asyncio
async def test_bacnet_defaults(bacnet):
    """All BACnet setpoints initialized at 24.0°C cooling default."""
    r = await bacnet.get("/objects")
    assert r.status_code == 200
    data = r.json()

    assert len(data) == 4  # 4 zones
    for zone_id, props in data.items():
        cooling = props.get("cooling_setpoint", {})
        assert cooling.get("present_value") == 24.0, \
            f"{zone_id} cooling setpoint should be 24.0°C at default"


# ---------------------------------------------------------------------------
# Scenario 2 — ML predictions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ml_predictions_structure(api):
    """Predictor returns valid output for all 4 zones."""
    r = await api.get("/dr/predict?lookahead_minutes=30")
    assert r.status_code == 200
    data = r.json()

    assert set(data.keys()) == {"zone_1", "zone_2", "zone_3", "zone_4"}

    for zone_id, pred in data.items():
        assert "predicted_occupancy" in pred
        assert "occupancy_ratio"     in pred
        assert "is_low_occupancy"    in pred
        assert "confidence"          in pred
        assert "capacity"            in pred

        assert pred["predicted_occupancy"] >= 0
        assert 0.0 <= pred["occupancy_ratio"] <= 1.0
        assert isinstance(pred["is_low_occupancy"], bool)
        assert 0.0 <= pred["confidence"] <= 1.0


@pytest.mark.asyncio
async def test_ml_predictions_lookahead_range(api):
    """Predictor accepts valid lookahead range 5–60 min."""
    for minutes in [5, 15, 30, 60]:
        r = await api.get(f"/dr/predict?lookahead_minutes={minutes}")
        assert r.status_code == 200, f"Failed for lookahead_minutes={minutes}"


# ---------------------------------------------------------------------------
# Scenario 3 — DR event lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dr_event_trigger(api):
    """DR event created with correct structure and active status."""
    payload = {
        "target_kw_reduction": 5.0,
        "duration_minutes":    5,
        "triggered_by":        "pytest",
        "notes":               "automated test"
    }
    r = await api.post("/dr/event", json=payload)
    assert r.status_code == 200
    data = r.json()

    assert data["status"] == "active"
    assert data["target_kw"] == 5.0
    assert data["duration_minutes"] == 5
    assert "event_id" in data
    assert "projected_kw_shed" in data
    assert "zones_affected" in data
    assert "zone_actions" in data
    assert len(data["zone_actions"]) == 4  # all zones evaluated

    # Optimizer should have found a valid plan
    assert data["projected_kw_shed"] >= 0


@pytest.mark.asyncio
async def test_dr_event_list(api):
    """DR events list endpoint returns valid records."""
    r = await api.get("/dr/events?limit=5")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)

    if data:
        event = data[0]
        assert "id"                  in event
        assert "status"              in event
        assert "target_kw_reduction" in event
        assert "duration_minutes"    in event
        assert "created_at"          in event


@pytest.mark.asyncio
async def test_dr_event_detail(api):
    """DR event detail endpoint returns event + zone actions."""
    # Get latest event ID
    r = await api.get("/dr/events?limit=1")
    assert r.status_code == 200
    events = r.json()

    if not events:
        pytest.skip("No DR events in DB yet")

    event_id = events[0]["id"]
    r = await api.get(f"/dr/events/{event_id}")
    assert r.status_code == 200
    data = r.json()

    assert "event"   in data
    assert "actions" in data
    assert data["event"]["id"] == event_id


@pytest.mark.asyncio
async def test_dr_event_setpoints_change(api, bacnet):
    """After DR trigger, BACnet setpoints change on affected zones."""
    # Trigger a DR event
    payload = {"target_kw_reduction": 8.0, "duration_minutes": 5, "triggered_by": "pytest"}
    r = await api.post("/dr/event", json=payload)
    assert r.status_code == 200
    event = r.json()

    zones_affected = event.get("zones_affected", [])
    if not zones_affected:
        pytest.skip("No zones affected — occupancy may be at edge case")

    # Check BACnet setpoints changed on at least one affected zone
    r = await bacnet.get("/objects")
    assert r.status_code == 200
    bacnet_data = r.json()

    changed = []
    for zone_id in zones_affected:
        sp = bacnet_data.get(zone_id, {}).get("cooling_setpoint", {})
        if sp.get("present_value", 24.0) > 24.0:
            changed.append(zone_id)

    assert len(changed) > 0, \
        f"Expected setpoints to change on zones {zones_affected}, none changed"


# ---------------------------------------------------------------------------
# Scenario 4 — Comfort constraint enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bacnet_rejects_out_of_range(bacnet):
    """BACnet device rejects setpoint above hard ceiling (28°C)."""
    payload = {
        "object_instance": 1,   # zone_1 cooling setpoint
        "value":           29.0,
        "written_by":      "pytest_constraint_test"
    }
    r = await bacnet.post("/write", json=payload)
    assert r.status_code == 422
    assert "VALUE_OUT_OF_RANGE" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_bacnet_rejects_below_minimum(bacnet):
    """BACnet device rejects cooling setpoint below floor (20°C)."""
    payload = {
        "object_instance": 1,
        "value":           15.0,
        "written_by":      "pytest_constraint_test"
    }
    r = await bacnet.post("/write", json=payload)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_dr_impossible_target_not_met(api):
    """100kW target returns target_met: false — optimizer respects physical limits."""
    payload = {"target_kw_reduction": 100.0, "duration_minutes": 5, "triggered_by": "pytest"}
    r = await api.post("/dr/event", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["target_met"] is False
    assert data["projected_kw_shed"] < 100.0


@pytest.mark.asyncio
async def test_comfort_status_endpoint(api):
    """Comfort status returned for all zones with correct fields."""
    r = await api.get("/zones/comfort")
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0

    for zone in data:
        assert "zone_id"      in zone
        assert "within_hard"  in zone
        assert "within_soft"  in zone
        assert "violations"   in zone
        assert "current_temp" in zone
        assert "current_co2"  in zone


# ---------------------------------------------------------------------------
# Scenario 5 — Closed-loop feedback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bacnet_write_accepted_in_range(bacnet):
    """BACnet accepts valid setpoint write (24–27°C range)."""
    payload = {
        "object_instance": 11,   # zone_2 cooling setpoint
        "value":           26.0,
        "written_by":      "pytest_feedback_test"
    }
    r = await bacnet.post("/write", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["value_after"] == 26.0
    assert data["value_before"] == 24.0 or data["value_before"] >= 24.0

    # Verify it persisted
    r2 = await bacnet.get("/objects/zone_2/cooling_setpoint")
    assert r2.status_code == 200
    assert r2.json()["present_value"] == 26.0


@pytest.mark.asyncio
async def test_bacnet_reset(bacnet):
    """Reset endpoint restores all setpoints to 24.0°C default."""
    # First raise one
    await bacnet.post("/write", json={"object_instance": 11, "value": 26.5, "written_by": "pytest"})

    # Reset all
    r = await bacnet.post("/reset")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # Verify restored
    r2 = await bacnet.get("/objects")
    data = r2.json()
    for zone_id, props in data.items():
        assert props["cooling_setpoint"]["present_value"] == 24.0, \
            f"{zone_id} should be reset to 24.0°C"


# ---------------------------------------------------------------------------
# Scenario 6 — Audit trail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_trail_has_entries(api):
    """Audit trail has entries with correct structure."""
    r = await api.get("/audit/?limit=20")
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0

    for entry in data:
        assert "event_type" in entry
        assert "severity"   in entry
        assert "message"    in entry
        assert "timestamp"  in entry
        assert entry["severity"] in {"info", "warn", "error"}


@pytest.mark.asyncio
async def test_audit_dr_events_logged(api):
    """DR trigger events appear in audit trail."""
    r = await api.get("/audit/?limit=50")
    assert r.status_code == 200
    entries = r.json()

    dr_entries = [e for e in entries if e["event_type"] in ("dr_triggered", "dr_completed")]
    assert len(dr_entries) > 0, "Expected DR audit entries from earlier test triggers"


@pytest.mark.asyncio
async def test_setpoint_log(api):
    """Setpoint log records BACnet writes from DR events."""
    r = await api.get("/audit/setpoints?limit=10")
    assert r.status_code == 200
    data = r.json()

    if data:
        entry = data[0]
        assert "zone_id"       in entry
        assert "value_before"  in entry
        assert "value_after"   in entry
        assert "source"        in entry


# ---------------------------------------------------------------------------
# Scenario 7 — TimescaleDB persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_building_summary_has_data(api):
    """Building summary returns all 5 sensor types with samples > 0."""
    r = await api.get("/readings/building/summary?minutes=60")
    assert r.status_code == 200
    data = r.json()

    assert "metrics" in data
    assert len(data["metrics"]) > 0

    sensor_types_returned = {m["sensor_type"] for m in data["metrics"]}
    expected = {"temperature", "humidity", "co2", "occupancy", "energy_kw"}
    assert expected.issubset(sensor_types_returned)

    for metric in data["metrics"]:
        assert metric["samples"] > 0, \
            f"Expected samples > 0 for {metric['sensor_type']}"


@pytest.mark.asyncio
async def test_zone_readings_time_series(api):
    """Zone time-series endpoint returns readings for a valid zone."""
    r = await api.get("/readings/zone_1?sensor_type=temperature&hours=1&limit=10")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)

    if data:
        reading = data[0]
        assert reading["zone_id"] == "zone_1"
        assert reading["sensor_type"] == "temperature"
        assert isinstance(reading["value"], float)
        assert "timestamp" in reading


@pytest.mark.asyncio
async def test_zone_readings_invalid_zone(api):
    """Unknown zone returns 404."""
    r = await api.get("/readings/zone_999?hours=1")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Zones endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zones_list(api):
    """All 4 zones returned with correct metadata."""
    r = await api.get("/zones/")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 4

    zone_ids = {z["zone_id"] for z in data}
    assert zone_ids == {"zone_1", "zone_2", "zone_3", "zone_4"}

    for zone in data:
        assert "zone_name" in zone
        assert "floor"     in zone
        assert "capacity"  in zone
        assert zone["capacity"] > 0


@pytest.mark.asyncio
async def test_setpoints_endpoint(api):
    """Current BACnet setpoints available via zones endpoint."""
    r = await api.get("/zones/setpoints")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
