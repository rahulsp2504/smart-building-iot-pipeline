"""
Smart Building DR Middleware — FastAPI Backend
===============================================
REST API + WebSocket live stream.

Endpoints:
  GET  /health
  GET  /zones/
  GET  /zones/comfort
  GET  /zones/setpoints
  GET  /readings/latest
  GET  /readings/building/summary
  GET  /readings/{zone_id}
  POST /dr/event                    ← trigger DR event
  GET  /dr/events
  GET  /dr/events/{event_id}
  GET  /dr/predict
  GET  /audit/
  GET  /audit/setpoints
  WS   /ws                          ← live sensor stream

Docs: /docs  (Swagger)  /redoc
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .database import check_db_connection
from .mqtt_bridge import register_ws, start, unregister_ws
from .ml.predictor import refresh_baselines, run_periodic_refresh
from .routes import readings_router, zones_router, dr_router, audit_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:3000,http://localhost:80",
).split(",")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting MQTT bridge…")
    bridge_task = await start()

    logger.info("Running initial ML baseline refresh…")
    try:
        await refresh_baselines()
    except Exception:
        logger.warning("Initial baseline refresh skipped (no data yet)")

    refresh_task = asyncio.create_task(run_periodic_refresh(1800))

    yield

    logger.info("Shutting down…")
    bridge_task.cancel()
    refresh_task.cancel()
    try:
        await bridge_task
        await refresh_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Smart Building DR Middleware",
    description=(
        "Comfort-constrained, occupancy-aware demand response middleware "
        "for MQTT-instrumented commercial buildings. "
        "Sensor pipeline → ML occupancy prediction → BACnet setpoint control → "
        "energy savings audit."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(readings_router)
app.include_router(zones_router)
app.include_router(dr_router)
app.include_router(audit_router)


@app.get("/health", tags=["meta"])
async def health():
    db_ok = await check_db_connection()
    return {
        "status":   "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "version":  "1.0.0",
    }


@app.websocket("/ws")
async def websocket_live(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"[WS] Client connected: {websocket.client}")
    q: asyncio.Queue = asyncio.Queue(maxsize=300)

    async def enqueue(msg: str):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    register_ws(enqueue)
    try:
        while True:
            msg = await q.get()
            await websocket.send_text(msg)
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected: {websocket.client}")
    finally:
        unregister_ws(enqueue)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
