from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from relay_agent.event_log import EventLog


STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    mode = os.environ.get("RELAY_MODE", "standard")
    events = EventLog()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        events.append("runtime.started", {"mode": mode})
        yield
        events.append("runtime.stopped", {"mode": mode})

    app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": mode}

    @app.get("/api/runtime")
    async def runtime() -> dict[str, str]:
        return {"mode": mode, "event_log": str(events.path)}

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app

