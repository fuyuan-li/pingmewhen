from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator, add_port, remove_port

from relay_agent.agentic_engine import AgenticTaskEngine
from relay_agent.call_capabilities import CallCapability, CallCapabilityStore, redact_capabilities
from relay_agent.context_store import ContextStore, InvalidContext
from relay_agent.credentials import CredentialStore, RelayCredentials
from relay_agent.event_log import EventLog, default_data_dir
from relay_agent.gatekeeper import Gatekeeper, OpenAIGatekeeper
from relay_agent.local_tts import LocalTTSRenderer, is_valid_sensitive_value, looks_like_protected_value
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
from relay_agent.task_errors import InvalidAction, TaskNotFound
from relay_agent.task_store import SQLiteTaskStore
from relay_agent.telephony import TERMINAL_CALL_STATUSES, TelephonyService, validate_twilio_signature
from relay_agent.tunnel import TunnelManager, public_health_ready


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


class TakeoverSayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


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
    tunnel_readiness_checker: Callable[[str], bool] | None = None,
    connection_status_delay: float = 0.45,
) -> FastAPI:
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

    def build_engine() -> AgenticTaskEngine:
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
    health_checker = tunnel_readiness_checker or public_health_ready
    execution_tasks: set[asyncio.Task] = set()
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
        user_input_requester=lambda task_id, question, input_kind, blocking, interaction_id, reason, representative_update: engine.request_user_input(
            task_id,
            question,
            input_kind,
            blocking,
            interaction_id,
            reason,
            representative_update,
        ),
        call_connected=lambda task_id: engine.mark_call_connected(task_id),
        tts_renderer=tts_renderer,
        realtime_model=lambda: model_settings.load().realtime_model,
        transcription_model=lambda: model_settings.load().transcription_model,
        session_update_timeout=3,
        response_delivery_timeout=float(os.environ.get("RELAY_RESPONSE_DELIVERY_TIMEOUT", "20")),
    )

    def setup_required() -> bool:
        return not resolved_credentials().complete

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

    async def execute_next_call(task_id: str, connection_status_started: bool = False) -> dict:
        pending = engine.next_phone_action(task_id)
        if pending is None:
            return engine.get(task_id)
        if not connection_status_started:
            engine.mark_connection_starting(task_id)
        if connection_status_delay > 0:
            await asyncio.sleep(connection_status_delay)
        public_url = tunnel.public_url
        events.append(
            "tunnel.health_check_started",
            {"task_id": task_id, "public_url": public_url, "tunnel_active": tunnel.active, "port": port},
        )
        healthy = False
        try:
            healthy = bool(public_url) and await asyncio.to_thread(health_checker, public_url)
        except Exception as error:
            events.append(
                "tunnel.health_check_error",
                {"task_id": task_id, "reason": type(error).__name__, "public_url": public_url, "port": port},
            )
        events.append(
            "tunnel.health_check_succeeded" if healthy else "tunnel.health_check_inconclusive",
            {"task_id": task_id, "public_url": public_url, "port": port},
        )
        engine.record_tunnel_health(task_id, healthy)
        if connection_status_delay > 0:
            await asyncio.sleep(connection_status_delay)
        engine.mark_call_starting(task_id)
        action = pending["action"]
        try:
            result = await asyncio.to_thread(
                telephony.place_call,
                action["phone_number"],
                task_id,
                pending["index"],
            )
            task = engine.begin_call(task_id, pending["index"], result["sid"])
        except Exception as error:
            events.append("call.failed", {"task_id": task_id, "reason": type(error).__name__})
            return engine.fail_execution(task_id, type(error).__name__)
        events.append("call.placed", {"task_id": task_id, "call_sid": result["sid"]})
        return task

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        events.append("runtime.started", {"mode": "standard"})

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
            for execution_task in execution_tasks:
                execution_task.cancel()
            if execution_tasks:
                await asyncio.gather(*execution_tasks, return_exceptions=True)
            if not warmup_task.done():
                warmup_task.cancel()
                await asyncio.gather(warmup_task, return_exceptions=True)
            tunnel.stop()
            events.append("runtime.stopped", {"mode": "standard"})

    app = FastAPI(title="PingMeWhen", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "standard"}

    @app.get("/api/runtime")
    async def runtime() -> dict:
        current_credentials = resolved_credentials()
        selected_models = model_settings.load()
        return {
            "mode": "standard",
            "event_log": str(events.path),
            "state_db": str(store.path),
            "workflow": "agentic-private-planning",
            "planner_ready": active_planner.ready,
            "planner_model": active_planner.model,
            "gatekeeper_model": selected_models.gatekeeper_model,
            "realtime_model": selected_models.realtime_model,
            "transcription_model": selected_models.transcription_model,
            "setup_required": setup_required(),
            "missing_credentials": current_credentials.missing,
            "credential_source": "local environment or machine-only file",
            "tunnel_active": tunnel.active,
            "tunnel_public_url": tunnel.public_url,
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
    async def attach_task_context(task_id: str, file: UploadFile = File(...), replan: bool = True) -> dict:
        try:
            metadata = contexts.save_pdf(file.filename or "context.pdf", await file.read())
            return engine.attach_context(task_id, metadata, replan=replan)
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

    @app.get("/api/tasks/{task_id}/listen-capability")
    async def listen_capability(task_id: str) -> dict[str, str]:
        try:
            task = engine.get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        current_call = task.get("current_call") or {}
        call_sid = str(current_call.get("call_sid", ""))
        capability = telephony.capabilities.active_for_task(task_id, call_sid) if call_sid else None
        if capability is None or task.get("phase") != "calling":
            raise HTTPException(status_code=409, detail="The live call audio stream is not available yet.")
        return {"websocket_path": f"/api/twilio/listen/{capability.listen_token}"}

    @app.post("/api/tasks/{task_id}/actions")
    async def act_on_task(task_id: str, request: TaskActionRequest) -> dict:
        try:
            was_calling = engine.get(task_id)["phase"] == "calling"
            if was_calling and request.action == "hangup":
                call_sid = engine.call_sid_for(task_id)
                if not call_sid:
                    raise InvalidAction("There is no active call to hang up.")
                events.append("realtime.hangup_requested", {"task_id": task_id})
                try:
                    await asyncio.to_thread(telephony.end_call, call_sid)
                except Exception as error:
                    events.append(
                        "realtime.hangup_failed",
                        {"task_id": task_id, "reason": type(error).__name__},
                    )
                    raise HTTPException(
                        status_code=502,
                        detail="PingMeWhen could not hang up the call. It may have already ended.",
                    ) from error
                snapshot = engine.hang_up_call_by_user(task_id)
                telephony.capabilities.revoke(call_sid)
                return snapshot
            if was_calling and request.action == "instruction":
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
                    return engine.record_call_delivery_failure(task_id, instruction)
                if not delivery:
                    events.append("realtime.instruction_rejected", {"task_id": task_id})
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The instruction was not delivered to the live call because PingMeWhen is disconnected or "
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
                    delivery.interaction_id,
                )
            task = engine.act(task_id, request.action, request.value)
            if task["stage"] == "execution_ready":
                checking = engine.mark_connection_starting(task_id)
                execution_task = asyncio.create_task(execute_next_call(task_id, connection_status_started=True))
                execution_tasks.add(execution_task)
                execution_task.add_done_callback(execution_tasks.discard)
                if connection_status_delay <= 0:
                    await execution_task
                return checking
            return task
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PlannerError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/takeover")
    async def begin_takeover(task_id: str) -> dict:
        try:
            task = engine.get(task_id)
            sensitive = bool(task.get("takeover_sensitive"))
            if task.get("takeover_active"):
                raise InvalidAction("Typed takeover is already active.")
            await realtime.enter_typed_takeover(task_id, sensitive=sensitive)
            return engine.begin_typed_takeover(task_id, sensitive=sensitive)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except (InvalidAction, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/tasks/{task_id}/takeover-say")
    async def takeover_say(task_id: str, request: TakeoverSayRequest) -> dict:
        try:
            task = engine.get(task_id)
            text = request.text.strip()
            if not text:
                raise InvalidAction("Type something for the representative.")
            if not task.get("takeover_active"):
                raise InvalidAction("Take over the active call before using type-to-speak.")
            if task.get("takeover_sensitive"):
                field = str(task.get("secure_expected_field") or "")
                if field == "verification_request" and looks_like_protected_value(text):
                    raise InvalidAction("PingMeWhen will not repeat a protected value. Type a non-sensitive response instead.")
                if field != "verification_request" and not is_valid_sensitive_value(field, text):
                    raise InvalidAction("Enter a valid value for the protected field PingMeWhen detected.")
            events.append(
                "call.takeover_speech_started",
                {"task_id": task_id, "sensitive": bool(task.get("takeover_sensitive"))},
            )
            await realtime.speak_takeover_text(task_id, text)
            request.text = ""
            return engine.mark_takeover_speech(task_id)
        except TaskNotFound as error:
            request.text = ""
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except (InvalidAction, RuntimeError) as error:
            request.text = ""
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            request.text = ""
            events.append("call.takeover_speech_failed", {"task_id": task_id, "reason": type(error).__name__})
            raise HTTPException(status_code=502, detail="Local type-to-speak did not complete.") from error

    @app.post("/api/tasks/{task_id}/resume-takeover")
    async def resume_takeover(task_id: str) -> dict:
        try:
            task = engine.get(task_id)
            if task.get("call_state") != "HUMAN_TAKEOVER" or not task.get("takeover_active"):
                raise InvalidAction("PingMeWhen can resume only after human takeover.")
            context_update = await realtime.exit_typed_takeover(task_id)
            return engine.resume_from_takeover(task_id, context_update)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail="Task not found.") from error
        except InvalidAction as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            events.append("call.takeover_resume_failed", {"task_id": task_id, "reason": type(error).__name__})
            raise HTTPException(status_code=502, detail="PingMeWhen could not safely return to the call.") from error

    @app.post("/api/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        _, capability = await validated_twilio_parameters(request, "voice")
        from twilio.twiml.voice_response import VoiceResponse

        if not capability.task_id:
            raise HTTPException(status_code=422, detail="This call is not attached to an approved PingMeWhen task.")
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
                if task_id and call_sid:
                    active = engine.get(task_id).get("current_call") or {}
                    if active.get("call_sid") == call_sid:
                        finished = engine.finish_call(task_id, call_sid, parameters["CallStatus"].lower())
                        if finished.get("stage") == "execution_ready":
                            await execute_next_call(task_id)
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

    @app.websocket("/api/twilio/listen/{capability_token}")
    async def listen_to_call(websocket: WebSocket, capability_token: str) -> None:
        capability = telephony.capabilities.authenticate("listen", capability_token)
        if capability is None:
            await websocket.close(code=1008)
            return
        try:
            await realtime.attach_listener(capability.task_id, websocket)
        except RuntimeError:
            await websocket.close(code=1013)
            return
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("text") is not None or message.get("bytes") is not None:
                    await websocket.close(code=1008)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            await realtime.detach_listener(capability.task_id, websocket)

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/previews")
    async def preview_gallery() -> FileResponse:
        return FileResponse(STATIC_DIR / "previews" / "index.html")

    @app.get("/previews/takeover")
    async def takeover_preview() -> FileResponse:
        return FileResponse(STATIC_DIR / "previews" / "takeover.html")

    @app.get("/previews/onboarding")
    async def onboarding_preview() -> FileResponse:
        return FileResponse(STATIC_DIR / "previews" / "onboarding.html")

    @app.get("/previews/preview.css")
    async def preview_styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "previews" / "preview.css", media_type="text/css")

    return app
