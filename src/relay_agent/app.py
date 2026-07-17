from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from relay_agent.agentic_engine import AgenticTaskEngine
from relay_agent.context_store import ContextStore, InvalidContext
from relay_agent.credentials import CredentialStore, RelayCredentials
from relay_agent.event_log import EventLog, default_data_dir
from relay_agent.planner import OpenAIPlanner, Planner, PlannerError, UnavailablePlanner
from relay_agent.realtime_bridge import RealtimeSessionHub
from relay_agent.task_engine import DeterministicTaskEngine, InvalidAction, TaskNotFound
from relay_agent.task_store import SQLiteTaskStore
from relay_agent.telephony import TERMINAL_CALL_STATUSES, TelephonyService, validate_twilio_signature
from relay_agent.tunnel import TunnelManager


STATIC_DIR = Path(__file__).parent / "static"


class CreateTaskRequest(BaseModel):
    goal: str
    contexts: list[dict] = Field(default_factory=list)


class TaskActionRequest(BaseModel):
    action: str
    value: str = ""


class CredentialSetupRequest(BaseModel):
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    openai_api_key: str = ""


def create_app(
    planner: Planner | None = None,
    credential_store: CredentialStore | None = None,
    tunnel_manager: TunnelManager | None = None,
    twilio_client_factory: Callable | None = None,
) -> FastAPI:
    mode = os.environ.get("RELAY_MODE", "standard")
    events = EventLog()
    contexts = ContextStore(events)
    store = SQLiteTaskStore(default_data_dir() / "state" / "relay.db")
    credentials = credential_store or CredentialStore()
    provided_planner = planner

    def resolved_credentials() -> RelayCredentials:
        return credentials.resolve()

    def configured_planner() -> Planner:
        if provided_planner is not None:
            return provided_planner
        api_key = resolved_credentials().openai_api_key
        return OpenAIPlanner(api_key) if api_key else UnavailablePlanner()

    active_planner = configured_planner()

    def build_engine() -> DeterministicTaskEngine | AgenticTaskEngine:
        if mode == "demo":
            return DeterministicTaskEngine(events, store, namespace="demo")
        return AgenticTaskEngine(events, active_planner, store, contexts.read_text)

    engine = build_engine()
    port = int(os.environ.get("RELAY_PORT", "8765"))
    tunnel = tunnel_manager or TunnelManager(port)
    telephony = TelephonyService(resolved_credentials, tunnel, twilio_client_factory)
    realtime = RealtimeSessionHub(
        resolved_credentials,
        lambda task_id, queue_index: engine.call_context(task_id, queue_index),
        lambda task_id, speaker, text: engine.append_transcript(task_id, speaker, text),
        events,
    )

    def setup_required() -> bool:
        return mode != "demo" and not resolved_credentials().complete

    async def validated_twilio_parameters(request: Request) -> dict[str, str]:
        signature = request.headers.get("X-Twilio-Signature", "")
        form = await request.form()
        parameters = {str(key): str(value) for key, value in form.items()}
        try:
            external_url = tunnel.url(request.url.path, request.url.query)
        except Exception as error:
            raise HTTPException(status_code=403, detail="Twilio request validation failed.") from error
        if not validate_twilio_signature(
            resolved_credentials().twilio_auth_token,
            external_url,
            parameters,
            signature,
        ):
            raise HTTPException(status_code=403, detail="Twilio request validation failed.")
        return parameters

    def execute_next_call(task_id: str) -> dict:
        if not isinstance(engine, AgenticTaskEngine):
            return engine.get(task_id)
        pending = engine.next_phone_action(task_id)
        if pending is None:
            return engine.get(task_id)
        action = pending["action"]
        try:
            result = telephony.place_call(action["phone_number"], task_id, pending["index"])
            task = engine.begin_call(task_id, pending["index"], result["sid"])
        except Exception as error:
            events.append("call.failed", {"task_id": task_id, "reason": type(error).__name__})
            return engine.fail_execution(task_id, type(error).__name__)
        events.append("call.placed", {"task_id": task_id, "call_sid": result["sid"]})
        return task

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        events.append("runtime.started", {"mode": mode})
        yield
        tunnel.stop()
        events.append("runtime.stopped", {"mode": mode})

    app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": mode}

    @app.get("/api/runtime")
    async def runtime() -> dict:
        current_credentials = resolved_credentials()
        return {
            "mode": mode,
            "event_log": str(events.path),
            "state_db": str(store.path),
            "workflow": "deterministic-insurance-preview" if mode == "demo" else "agentic-private-planning",
            "planner_ready": True if mode == "demo" else active_planner.ready,
            "planner_model": "deterministic" if mode == "demo" else active_planner.model,
            "setup_required": setup_required(),
            "missing_credentials": [] if mode == "demo" else current_credentials.missing,
            "credential_source": "not required" if mode == "demo" else "local environment or machine-only file",
        }

    @app.post("/api/setup")
    async def save_setup(request: CredentialSetupRequest) -> dict:
        nonlocal active_planner, engine
        existing = resolved_credentials().as_environment()
        entered = RelayCredentials(
            twilio_account_sid=request.twilio_account_sid.strip(),
            twilio_auth_token=request.twilio_auth_token.strip(),
            twilio_from_number=request.twilio_from_number.strip(),
            openai_api_key=request.openai_api_key.strip(),
        )
        combined = RelayCredentials(
            twilio_account_sid=request.twilio_account_sid.strip() or existing["TWILIO_ACCOUNT_SID"],
            twilio_auth_token=request.twilio_auth_token.strip() or existing["TWILIO_AUTH_TOKEN"],
            twilio_from_number=request.twilio_from_number.strip() or existing["TWILIO_FROM_NUMBER"],
            openai_api_key=request.openai_api_key.strip() or existing["OPENAI_API_KEY"],
        )
        if not combined.complete:
            raise HTTPException(
                status_code=422,
                detail=f"Missing required credentials: {', '.join(combined.missing)}",
            )
        stored = credentials.load()
        persisted = RelayCredentials(
            twilio_account_sid=entered.twilio_account_sid or stored.twilio_account_sid,
            twilio_auth_token=entered.twilio_auth_token or stored.twilio_auth_token,
            twilio_from_number=entered.twilio_from_number or stored.twilio_from_number,
            openai_api_key=entered.openai_api_key or stored.openai_api_key,
        )
        try:
            credentials.save(persisted, require_complete=False)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        active_planner = configured_planner()
        engine = build_engine()
        entered_names = [name for name, value in entered.as_environment().items() if value]
        events.append("runtime.credentials_configured", {"credential_names": entered_names})
        return {"configured": True, "setup_required": False, "planner_ready": active_planner.ready}

    @app.post("/api/tasks")
    async def create_task(request: CreateTaskRequest) -> dict:
        if setup_required():
            raise HTTPException(status_code=428, detail="Complete local credential setup before starting a task.")
        try:
            return engine.create(request.goal, request.contexts)
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except InvalidAction as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/contexts")
    async def upload_context(file: UploadFile = File(...)) -> dict:
        if setup_required():
            raise HTTPException(status_code=428, detail="Complete local credential setup before uploading context.")
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
            was_calling = engine.get(task_id)["phase"] == "calling"
            task = engine.act(task_id, request.action, request.value)
            if was_calling and request.action == "instruction" and isinstance(engine, AgenticTaskEngine):
                await realtime.inject(task_id, request.value.strip())
            if isinstance(engine, AgenticTaskEngine) and task["stage"] == "execution_ready":
                return execute_next_call(task_id)
            return task
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        await validated_twilio_parameters(request)
        from twilio.twiml.voice_response import VoiceResponse

        task_id = request.query_params.get("task_id", "")
        queue_index = request.query_params.get("queue_index", "")
        if not task_id or not queue_index.isdigit():
            raise HTTPException(status_code=422, detail="This call is not attached to an approved Relay task.")
        stream_url = tunnel.url("/api/twilio/media").replace("https://", "wss://", 1)
        response = VoiceResponse()
        stream = response.connect().stream(url=stream_url)
        stream.parameter(name="task_id", value=task_id)
        stream.parameter(name="queue_index", value=queue_index)
        return Response(content=str(response), media_type="application/xml")

    @app.post("/api/twilio/status")
    async def twilio_status(request: Request) -> dict[str, bool]:
        parameters = await validated_twilio_parameters(request)
        if parameters.get("CallStatus", "").lower() in TERMINAL_CALL_STATUSES:
            task_id = request.query_params.get("task_id", "")
            call_sid = parameters.get("CallSid", "")
            if task_id and call_sid and isinstance(engine, AgenticTaskEngine):
                active = engine.get(task_id).get("current_call") or {}
                if active.get("call_sid") == call_sid:
                    try:
                        engine.finish_call(task_id, call_sid, parameters["CallStatus"].lower())
                        execute_next_call(task_id)
                    finally:
                        tunnel.release()
        return {"accepted": True}

    @app.websocket("/api/twilio/media")
    async def twilio_media(websocket: WebSocket) -> None:
        signature = websocket.headers.get("X-Twilio-Signature", "")
        try:
            external_url = tunnel.url("/api/twilio/media").replace("https://", "wss://", 1)
        except Exception:
            await websocket.close(code=1008)
            return
        if not validate_twilio_signature(
            resolved_credentials().twilio_auth_token,
            external_url,
            {},
            signature,
        ):
            await websocket.close(code=1008)
            return
        await realtime.bridge(websocket)

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
