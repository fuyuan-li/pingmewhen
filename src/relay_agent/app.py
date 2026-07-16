from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from relay_agent.agentic_engine import AgenticTaskEngine
from relay_agent.context_store import ContextStore, InvalidContext
from relay_agent.event_log import EventLog, default_data_dir
from relay_agent.planner import Planner, PlannerError, planner_from_environment
from relay_agent.task_engine import DeterministicTaskEngine, InvalidAction, TaskNotFound
from relay_agent.task_store import SQLiteTaskStore


STATIC_DIR = Path(__file__).parent / "static"


class CreateTaskRequest(BaseModel):
    goal: str
    contexts: list[dict] = Field(default_factory=list)


class TaskActionRequest(BaseModel):
    action: str
    value: str = ""


def create_app(planner: Planner | None = None) -> FastAPI:
    mode = os.environ.get("RELAY_MODE", "standard")
    events = EventLog()
    contexts = ContextStore(events)
    store = SQLiteTaskStore(default_data_dir() / "state" / "relay.db")
    active_planner = planner or planner_from_environment()
    engine = (
        DeterministicTaskEngine(events, store, namespace="demo")
        if mode == "demo"
        else AgenticTaskEngine(events, active_planner, store, contexts.read_text)
    )

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
    async def runtime() -> dict[str, str | bool]:
        return {
            "mode": mode,
            "event_log": str(events.path),
            "state_db": str(store.path),
            "workflow": "deterministic-insurance-preview" if mode == "demo" else "agentic-private-planning",
            "planner_ready": True if mode == "demo" else active_planner.ready,
            "planner_model": "deterministic" if mode == "demo" else active_planner.model,
        }

    @app.post("/api/tasks")
    async def create_task(request: CreateTaskRequest) -> dict:
        try:
            return engine.create(request.goal, request.contexts)
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except InvalidAction as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/contexts")
    async def upload_context(file: UploadFile = File(...)) -> dict:
        try:
            return contexts.save_pdf(file.filename or "context.pdf", await file.read())
        except InvalidContext as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/contexts")
    async def attach_task_context(task_id: str, file: UploadFile = File(...)) -> dict:
        try:
            metadata = contexts.save_pdf(file.filename or "context.pdf", await file.read())
            return engine.attach_context(task_id, metadata)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidContext as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        try:
            return engine.get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error

    @app.post("/api/tasks/{task_id}/actions")
    async def act_on_task(task_id: str, request: TaskActionRequest) -> dict:
        try:
            return engine.act(task_id, request.action, request.value)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
