from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator, add_port, remove_port

from relay_agent.agentic_engine import AgenticTaskEngine
from relay_agent.call_capabilities import CallCapability, CallCapabilityStore, redact_capabilities
from relay_agent.context_store import ContextStore, InvalidContext
from relay_agent.credentials import CredentialStore, RelayCredentials
from relay_agent.event_log import EventLog, default_data_dir
from relay_agent.gatekeeper import Gatekeeper, OpenAIGatekeeper
from relay_agent.local_tts import LocalTTSRenderer, is_allowed_fake_value
from relay_agent.model_settings import (
    GATEKEEPER_MODELS,
    PLANNING_MODELS,
    REALTIME_MODELS,
    TRANSCRIPTION_MODELS,
    ModelSettings,
    ModelSettingsStore,
)
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


class SecureFieldRequest(BaseModel):
    field: str
    value: str


class ModelSettingsRequest(BaseModel):
    planning_model: str
    gatekeeper_model: str
    realtime_model: str
    transcription_model: str


def create_app(
    planner: Planner | None = None,
    credential_store: CredentialStore | None = None,
    tunnel_manager: TunnelManager | None = None,
    twilio_client_factory: Callable | None = None,
    tts_renderer: LocalTTSRenderer | None = None,
    model_settings_store: ModelSettingsStore | None = None,
    capability_store: CallCapabilityStore | None = None,
    gatekeeper: Gatekeeper | None = None,
) -> FastAPI:
    mode = os.environ.get("RELAY_MODE", "standard")
    events = EventLog()
    contexts = ContextStore(events)
    store = SQLiteTaskStore(default_data_dir() / "state" / "relay.db")
    credentials = credential_store or CredentialStore()
    model_settings = model_settings_store or ModelSettingsStore()
    provided_planner = planner

    def resolved_credentials() -> RelayCredentials:
        return credentials.resolve()

    def configured_planner() -> Planner:
        if provided_planner is not None:
            return provided_planner
        api_key = resolved_credentials().openai_api_key
        return OpenAIPlanner(api_key, model_settings.load().planning_model) if api_key else UnavailablePlanner()

    active_planner = configured_planner()

    def build_engine() -> DeterministicTaskEngine | AgenticTaskEngine:
        if mode == "demo":
            return DeterministicTaskEngine(events, store, namespace="demo")
        return AgenticTaskEngine(events, active_planner, store, contexts.read_text)

    engine = build_engine()
    port = int(os.environ.get("RELAY_PORT", "8765"))
    tunnel = tunnel_manager or TunnelManager(port)
    telephony = TelephonyService(
        resolved_credentials,
        tunnel,
        twilio_client_factory,
        capabilities=capability_store,
    )
    active_gatekeeper = gatekeeper or OpenAIGatekeeper(
        lambda: resolved_credentials().openai_api_key,
        lambda: model_settings.load().gatekeeper_model,
    )
    realtime = RealtimeSessionHub(
        resolved_credentials,
        lambda task_id, queue_index: engine.call_context(task_id, queue_index),
        lambda task_id, speaker, text: engine.append_transcript(task_id, speaker, text),
        events,
        gatekeeper=active_gatekeeper,
        secure_requester=lambda task_id, field_name: engine.request_secure_field(task_id, field_name),
        user_input_requester=lambda task_id, question, input_kind, blocking: engine.request_user_input(
            task_id, question, input_kind, blocking
        ),
        call_connected=lambda task_id: engine.mark_call_connected(task_id),
        tts_renderer=tts_renderer,
        realtime_model=lambda: model_settings.load().realtime_model,
        transcription_model=lambda: model_settings.load().transcription_model,
        session_update_timeout=3,
    )

    def setup_required() -> bool:
        return mode != "demo" and not resolved_credentials().complete

    async def validated_twilio_parameters(
        request: Request,
        scope: str,
    ) -> tuple[dict[str, str], CallCapability]:
        signature = request.headers.get("X-Twilio-Signature", "")
        form = await request.form()
        parameters = {str(key): str(value) for key, value in form.items()}
        capability_token = request.query_params.get("capability", "")
        capability = telephony.capabilities.authenticate(
            scope,
            capability_token,
            parameters.get("AccountSid", ""),
            parameters.get("CallSid", ""),
        )
        query_task_id = request.query_params.get("task_id", "")
        query_queue_index = request.query_params.get("queue_index", "")
        identity_matches = capability is not None
        if capability is not None and query_task_id and query_task_id != capability.task_id:
            identity_matches = False
        if capability is not None and query_queue_index:
            identity_matches = identity_matches and query_queue_index.isdigit()
            identity_matches = identity_matches and int(query_queue_index) == capability.queue_index
        if not identity_matches or capability is None:
            events.append(
                "twilio.capability_rejected",
                {
                    "path": request.url.path,
                    "scope": scope,
                    "parameter_names": sorted({str(key) for key, _ in form.multi_items()}),
                    "capability_present": bool(capability_token),
                },
            )
            raise HTTPException(status_code=403, detail="Twilio request authentication failed.")
        try:
            external_url = tunnel.url(request.url.path, request.url.query)
        except Exception as error:
            raise HTTPException(status_code=403, detail="Twilio request validation failed.") from error
        if signature and not validate_twilio_signature(
            resolved_credentials().twilio_auth_token,
            external_url,
            form,
            signature,
        ):
            validator = RequestValidator(resolved_credentials().twilio_auth_token)
            parsed_external_url = urlparse(external_url)
            url_with_port = add_port(parsed_external_url)
            url_without_port = remove_port(parsed_external_url)
            events.append(
                "twilio.signature_rejected",
                {
                    "path": request.url.path,
                    "parameter_names": sorted({str(key) for key, _ in form.multi_items()}),
                    "external_url": redact_capabilities(external_url),
                    "raw_query_string": redact_capabilities(request.url.query),
                    "received_signature": signature,
                    "url_with_port": redact_capabilities(url_with_port),
                    "computed_signature_with_port": validator.compute_signature(url_with_port, form),
                    "url_without_port": redact_capabilities(url_without_port),
                    "computed_signature_without_port": validator.compute_signature(url_without_port, form),
                },
            )
            raise HTTPException(status_code=403, detail="Twilio request validation failed.")
        return parameters, capability

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
        warmup_task = None
        if mode != "demo":
            async def warm_tunnel() -> None:
                try:
                    public_url = await asyncio.to_thread(tunnel.acquire)
                    events.append("tunnel.started", {"public_url": public_url, "port": port})
                except Exception as error:
                    events.append("tunnel.failed", {"reason": type(error).__name__, "port": port})

            warmup_task = asyncio.create_task(warm_tunnel())
        try:
            yield
        finally:
            if warmup_task is not None and not warmup_task.done():
                warmup_task.cancel()
                await asyncio.gather(warmup_task, return_exceptions=True)
            tunnel.stop()
            events.append("runtime.stopped", {"mode": mode})

    app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": mode}

    @app.get("/api/runtime")
    async def runtime() -> dict:
        current_credentials = resolved_credentials()
        selected_models = model_settings.load()
        return {
            "mode": mode,
            "event_log": str(events.path),
            "state_db": str(store.path),
            "workflow": "deterministic-insurance-preview" if mode == "demo" else "agentic-private-planning",
            "planner_ready": True if mode == "demo" else active_planner.ready,
            "planner_model": "deterministic" if mode == "demo" else active_planner.model,
            "gatekeeper_model": "deterministic" if mode == "demo" else selected_models.gatekeeper_model,
            "realtime_model": "deterministic" if mode == "demo" else selected_models.realtime_model,
            "transcription_model": "deterministic" if mode == "demo" else selected_models.transcription_model,
            "setup_required": setup_required(),
            "missing_credentials": [] if mode == "demo" else current_credentials.missing,
            "credential_source": "not required" if mode == "demo" else "local environment or machine-only file",
            "tunnel_active": False if mode == "demo" else tunnel.active,
            "tunnel_public_url": "" if mode == "demo" else tunnel.public_url,
        }

    @app.get("/api/model-settings")
    async def get_model_settings() -> dict:
        selected = model_settings.load()
        return {
            **selected.__dict__,
            "options": {
                "planning": list(PLANNING_MODELS),
                "gatekeeper": list(GATEKEEPER_MODELS),
                "realtime": list(REALTIME_MODELS),
                "transcription": list(TRANSCRIPTION_MODELS),
            },
        }

    @app.put("/api/model-settings")
    async def save_model_settings(request: ModelSettingsRequest) -> dict:
        nonlocal active_planner, engine
        selected = ModelSettings(
            planning_model=request.planning_model.strip(),
            gatekeeper_model=request.gatekeeper_model.strip(),
            realtime_model=request.realtime_model.strip(),
            transcription_model=request.transcription_model.strip(),
        )
        try:
            model_settings.save(selected)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        active_planner = configured_planner()
        engine = build_engine()
        events.append("runtime.models_configured", selected.__dict__)
        return {**selected.__dict__, "saved": True}

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
            if was_calling and request.action == "instruction" and isinstance(engine, AgenticTaskEngine):
                instruction = request.value.strip()
                if not instruction:
                    raise InvalidAction("Type a message before sending it.")
                events.append("realtime.instruction_received", {"task_id": task_id})
                try:
                    delivery = await realtime.inject(task_id, instruction)
                except Exception as error:
                    events.append(
                        "realtime.instruction_failed",
                        {"task_id": task_id, "reason": type(error).__name__},
                    )
                    raise HTTPException(
                        status_code=502,
                        detail="Relay received the private message but could not apply it to the active call.",
                    ) from error
                if not delivery:
                    events.append("realtime.instruction_rejected", {"task_id": task_id})
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The instruction was not delivered to the live call because Relay is disconnected or "
                            "its Realtime participation is paused."
                        ),
                    )
                return engine.record_call_private_exchange(
                    task_id,
                    instruction,
                    delivery.disposition,
                    delivery.context_update,
                    delivery.private_reply,
                    delivery.resumed_call,
                )
            task = engine.act(task_id, request.action, request.value)
            if isinstance(engine, AgenticTaskEngine) and task["stage"] == "execution_ready":
                return execute_next_call(task_id)
            return task
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/secure-fields")
    async def speak_secure_field(task_id: str, request: SecureFieldRequest) -> dict:
        if not isinstance(engine, AgenticTaskEngine):
            raise HTTPException(status_code=409, detail="Real secure voice is available only during a production call.")
        try:
            task = engine.get(task_id)
            if (
                not task.get("secure_mode")
                or task.get("call_state") != "SECURE_LOCAL"
                or task.get("secure_expected_field") != request.field
            ):
                raise InvalidAction("Relay is not waiting for that secure field.")
            if not is_allowed_fake_value(request.field, request.value):
                raise InvalidAction("P0 secure local voice accepts only the displayed fake test value.")
            await realtime.speak_secure_field(task_id, request.field, request.value)
            request.value = ""
            await realtime.resume_after_secure_field(task_id)
            return engine.complete_secure_field(task_id, request.field)
        except TaskNotFound as error:
            request.value = ""
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            request.value = ""
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            request.value = ""
            events.append("secure_mode.failed", {"task_id": task_id, "reason": type(error).__name__})
            raise HTTPException(status_code=502, detail="The protected local voice exchange did not complete.") from error

    @app.post("/api/tasks/{task_id}/resume-takeover")
    async def resume_takeover(task_id: str) -> dict:
        if not isinstance(engine, AgenticTaskEngine):
            raise HTTPException(status_code=409, detail="Real call takeover resume is available only in production.")
        try:
            task = engine.get(task_id)
            if task.get("call_state") != "HUMAN_TAKEOVER":
                raise InvalidAction("Relay can resume only after human takeover.")
            await realtime.resume_from_takeover(task_id)
            return engine.resume_from_takeover(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        _, capability = await validated_twilio_parameters(request, "voice")
        from twilio.twiml.voice_response import VoiceResponse

        if not capability.task_id:
            raise HTTPException(status_code=422, detail="This call is not attached to an approved Relay task.")
        stream_url = tunnel.url(f"/api/twilio/media/{capability.media_token}").replace("https://", "wss://", 1)
        response = VoiceResponse()
        stream = response.connect().stream(url=stream_url)
        stream.parameter(name="task_id", value=capability.task_id)
        stream.parameter(name="queue_index", value=str(capability.queue_index))
        return Response(content=str(response), media_type="application/xml")

    @app.post("/api/twilio/status")
    async def twilio_status(request: Request) -> dict[str, bool]:
        parameters, capability = await validated_twilio_parameters(request, "status")
        if parameters.get("CallStatus", "").lower() in TERMINAL_CALL_STATUSES:
            task_id = capability.task_id
            call_sid = parameters.get("CallSid", "")
            try:
                if task_id and call_sid and isinstance(engine, AgenticTaskEngine):
                    active = engine.get(task_id).get("current_call") or {}
                    if active.get("call_sid") == call_sid:
                        finished = engine.finish_call(task_id, call_sid, parameters["CallStatus"].lower())
                        if finished.get("stage") == "execution_ready":
                            execute_next_call(task_id)
            finally:
                telephony.capabilities.revoke(call_sid)
                tunnel.release()
        return {"accepted": True}

    @app.websocket("/api/twilio/media")
    async def twilio_media_without_capability(websocket: WebSocket) -> None:
        events.append(
            "media.connection_attempt",
            {"path": websocket.url.path, "capability_present": False},
        )
        events.append(
            "media.capability_rejected",
            {"path": websocket.url.path, "capability_present": False, "reason": "missing"},
        )
        await websocket.close(code=1008)

    @app.websocket("/api/twilio/media/{capability_token}")
    async def twilio_media(websocket: WebSocket, capability_token: str) -> None:
        redacted_path = redact_capabilities(websocket.url.path)
        events.append(
            "media.connection_attempt",
            {"path": redacted_path, "capability_present": bool(capability_token)},
        )
        capability = telephony.capabilities.authenticate("media", capability_token)
        if capability is None:
            events.append(
                "media.capability_rejected",
                {"path": redacted_path, "capability_present": bool(capability_token), "reason": "not_active"},
            )
            await websocket.close(code=1008)
            return
        signature = websocket.headers.get("X-Twilio-Signature", "")
        try:
            external_url = tunnel.url(f"/api/twilio/media/{capability_token}").replace("https://", "wss://", 1)
        except Exception:
            await websocket.close(code=1008)
            return
        if signature and not validate_twilio_signature(
            resolved_credentials().twilio_auth_token,
            external_url,
            {},
            signature,
        ):
            await websocket.close(code=1008)
            return
        await realtime.bridge(
            websocket,
            expected_task_id=capability.task_id,
            expected_queue_index=capability.queue_index,
            expected_call_sid=capability.call_sid,
        )

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
