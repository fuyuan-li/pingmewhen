from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from relay_agent.call_debug import CallDebugTrace
from relay_agent.credentials import RelayCredentials
from relay_agent.event_log import EventLog
from relay_agent.gatekeeper import (
    AllowAllGatekeeper,
    Gatekeeper,
    GatekeeperRequest,
    GatekeeperVerdict,
    gatekeeper_request,
)
from relay_agent.local_tts import LocalTTSRenderer, MacOSLocalTTS


SENSITIVE_FIELD_PATTERNS = {
    "card_number": (r"\b(?:credit|debit|payment)?\s*card\s+number\b", r"\b(?:\d[ -]?){13,19}\b"),
    "expiration": (r"\bexpir(?:ation|y|es)\b", r"\bexp(?:iry)?\s*(?:date)?\b"),
    "cvv": (r"\bcvv\b", r"\bcvc\b", r"\bsecurity\s+code\b"),
    "full_ssn": (
        r"\bfull\s+(?:social security|ssn)\b",
        r"\bsocial security (?:number|no\.?|#)\b",
        r"\b\d{3}[ -]\d{2}[ -]\d{4}\b",
    ),
}


def requested_sensitive_field(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\b(?:repeat|read back|verify)\b.{0,40}\b(?:that|it|card|number|details?)\b", lowered):
        return "verification_request"
    if re.search(r"\blast\s+(?:four|4)\b.{0,20}\b(?:social|ssn)\b", lowered):
        return None
    for field_name, patterns in SENSITIVE_FIELD_PATTERNS.items():
        if any(re.search(pattern, lowered) for pattern in patterns):
            return field_name
    return None


@dataclass
class ActiveRealtimeSession:
    realtime: Any
    twilio: WebSocket
    stream_sid: str
    secure_mode: bool = False
    waiting_for_user: bool = False
    pending_tool_call_id: str | None = None
    expected_field: str | None = None
    mark_events: dict[str, asyncio.Event] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    updates_from_user: list[str] = field(default_factory=list)
    hold_response_active: bool = False
    hold_complete: asyncio.Event = field(default_factory=asyncio.Event)
    debug_trace: CallDebugTrace | None = None


def realtime_session_update(
    context: dict[str, Any], transcription_model: str = "gpt-4o-mini-transcribe"
) -> dict[str, Any]:
    action = context["action"]
    caller_name = str(context.get("caller_name", "")).strip() or "the user"
    caller_name_literal = json.dumps(caller_name)
    target_literal = json.dumps(action["target"])
    private_context = "\n".join(context.get("private_messages", [])[-20:])
    document_context = context.get("document_context", "")
    prior_calls = context.get("prior_call_transcript", "")
    instructions = (
        "IDENTITY — read this first, keep these two facts separate for the whole call:\n"
        f"- You represent (you are calling ON BEHALF OF): {caller_name_literal}\n"
        f"- You are calling (the organization/person you dial and speak to): {target_literal}\n"
        f"- Never say you are calling {caller_name_literal}. That is who you represent, not who you are calling.\n"
        f"- Never say you are {caller_name_literal} or claim to be them. You represent them; you are not them.\n\n"
        "ROLE: You are Relay, the outbound caller who initiated this call to accomplish a specific approved goal. "
        "You are not an inbound support agent: never welcome the other person to a support service, ask what they "
        "called about, or offer generic help. Your spoken audio is always addressed to the representative you "
        "called. Private text comes from the person you represent; it is an answer or instruction for you, not "
        "speech from the representative. Never acknowledge or answer that private person aloud as though they were "
        "on the phone. Reformulate their information naturally for the representative.\n\n"
        "OPENING (say once, at the very start of the call, then never again): lead with the human — say who you "
        "are calling on behalf of — then briefly disclose that you are Relay, an AI assistant helping them by voice "
        "while they follow along by text. Warm, low-key, no more than two natural sentences. Do not ask whether the "
        "representative is comfortable continuing. After this opening, never repeat the disclosure, either identity "
        "fact above, your introduction, or the full purpose unless the representative explicitly asks. Never deny "
        "being an AI if asked.\n\n"
        "GOAL: Pursue only this approved purpose: "
        f"{action['purpose']}. The overall user goal is: {context['goal']}. Continue each turn naturally from the "
        "immediately preceding conversation.\n\n"
        "TURN CONTROL: The backend Gatekeeper reviews each representative turn and creates your response only when "
        "you may answer from known context. Do not second-guess an approved turn merely because you would phrase it "
        "differently. The request_user_input tool is fallback only: use it if, despite Gatekeeper approval, you "
        "discover a critical missing fact or decision. In that fallback, first say one brief natural hold line, then "
        "call request_user_input with the exact concise private question, input_kind='text', and blocking=true. Do "
        "not continue until the tool result arrives. Never fabricate an answer or ask the representative to supply "
        "the user's missing fact.\n\n"
        "BOUNDARIES: Never claim to be the user. Do not provide payment-card data or a full Social Security number. "
        "Do not choose a regulated product for the user. Be concise and natural.\n\n"
        "CONTEXT — the user's private task messages are:\n"
        f"{private_context}\nRelevant local document text is:\n{document_context}\nPrior separate call transcript for task "
        f"memory only:\n{prior_calls}\nTreat this as a new representative who has not heard any prior call."
    )
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "tools": [
                {
                    "type": "function",
                    "name": "request_user_input",
                    "description": (
                        "Ask the user following the call by text for one missing fact or decision required to continue."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "A concise question containing exactly what the user must answer.",
                            },
                            "input_kind": {"type": "string", "enum": ["text"]},
                            "blocking": {"type": "boolean"},
                        },
                        "required": ["question", "input_kind", "blocking"],
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": transcription_model, "language": "en"},
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": True,
                    },
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": "marin"},
            },
        },
    }


def initial_response(context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    action = context.get("action", {})
    caller_name = str(context.get("caller_name", "")).strip() or "the user"
    target = action.get("target", "the representative")
    return {
        "type": "response.create",
        "response": {
            "instructions": (
                f"Open this outbound call now. You are calling {target}. You represent {caller_name} — say you are "
                f"calling on behalf of {caller_name}, do not say you are calling {caller_name}. Briefly disclose once "
                "that you are Relay, an AI assistant helping them by voice while they follow by text, then "
                f"immediately state the concrete reason for calling: {action.get('purpose', 'the approved purpose')}. "
                "Do not welcome the recipient, offer generic support, ask what they want to do, or ask permission to "
                "continue."
            ),
        },
    }


def private_instruction(text: str) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Private text from the person you represent, not from the representative on the phone: "
                        f"{text}"
                    ),
                }
            ],
        },
    }


def transcript_from_realtime_event(event: dict[str, Any]) -> tuple[str, str] | None:
    event_type = event.get("type")
    if event_type == "response.output_audio_transcript.done":
        return "relay", str(event.get("transcript", ""))
    if event_type == "conversation.item.input_audio_transcription.completed":
        return "representative", str(event.get("transcript", ""))
    return None


class RealtimeSessionHub:
    def __init__(
        self,
        credentials: Callable[[], RelayCredentials],
        context_reader: Callable[[str, int], dict[str, Any]],
        transcript_writer: Callable[[str, str, str], dict[str, Any]],
        events: EventLog,
        connector: Callable[..., Any] = connect,
        gatekeeper: Gatekeeper | None = None,
        secure_requester: Callable[[str, str], dict[str, Any]] | None = None,
        user_input_requester: Callable[[str, str, str, bool], dict[str, Any]] | None = None,
        call_connected: Callable[[str], dict[str, Any]] | None = None,
        tts_renderer: LocalTTSRenderer | None = None,
        playback_timeout: float = 20,
        realtime_model: Callable[[], str] | None = None,
        transcription_model: Callable[[], str] | None = None,
    ) -> None:
        self._credentials = credentials
        self._context_reader = context_reader
        self._transcript_writer = transcript_writer
        self._events = events
        self._connector = connector
        self._gatekeeper = gatekeeper or AllowAllGatekeeper()
        self._secure_requester = secure_requester
        self._user_input_requester = user_input_requester
        self._call_connected = call_connected
        self._tts_renderer = tts_renderer or MacOSLocalTTS()
        self._playback_timeout = playback_timeout
        self._realtime_model = realtime_model or (lambda: "gpt-realtime-2.1-mini")
        self._transcription_model = transcription_model or (lambda: "gpt-4o-mini-transcribe")
        self._sessions: dict[str, ActiveRealtimeSession] = {}
        self._lock = asyncio.Lock()

    async def inject(self, task_id: str, text: str) -> bool:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None or session.secure_mode:
            return False
        waiting_for_user = session.waiting_for_user
        pending_call_id = session.pending_tool_call_id
        if waiting_for_user and pending_call_id:
            await session.realtime.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": pending_call_id,
                            "output": json.dumps({"status": "answered"}),
                        },
                    }
                )
            )
        if session.hold_response_active:
            try:
                await asyncio.wait_for(session.hold_complete.wait(), timeout=3)
            except TimeoutError:
                await session.realtime.send(json.dumps({"type": "response.cancel"}))
            session.hold_response_active = False
            session.hold_complete.set()
        await session.realtime.send(json.dumps(private_instruction(text)))
        session.waiting_for_user = False
        session.pending_tool_call_id = None
        try:
            await session.realtime.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "instructions": (
                                "The person you represent answered privately. You are still speaking aloud to the "
                                "representative you called, not to that private person. Do not acknowledge the private "
                                "answer with phrases such as 'Got it,' and do not address its author as 'you.' "
                                "Reformulate the answer as information for the representative, answering their pending "
                                "question if there is one, then continue the active phone conversation naturally. If "
                                "another required fact is missing, use request_user_input again."
                                if waiting_for_user
                                else (
                                    "This is a private direction from the person you represent. Continue speaking to "
                                    "the representative you called. Do not acknowledge or answer the private person "
                                    "aloud, and do not mention the private channel. Apply or reformulate the direction "
                                    "naturally for the representative."
                                )
                            )
                        },
                    }
                )
            )
        except Exception:
            session.waiting_for_user = waiting_for_user
            session.pending_tool_call_id = pending_call_id
            raise
        session.updates_from_user.append(text)
        self._events.append("realtime.instruction_injected", {"task_id": task_id})
        return True

    async def speak_secure_field(self, task_id: str, field_name: str, value: str) -> None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None or not session.secure_mode or session.expected_field != field_name:
            raise RuntimeError("Relay is not waiting for that secure field.")
        chunks = self._tts_renderer.render(field_name, value)
        value = ""
        if not chunks:
            raise RuntimeError("Local speech returned no audio.")
        mark_name = f"secure-{uuid4().hex}"
        completion = asyncio.Event()
        session.mark_events[mark_name] = completion
        try:
            for payload in chunks:
                await session.twilio.send_json(
                    {"event": "media", "streamSid": session.stream_sid, "media": {"payload": payload}}
                )
            await session.twilio.send_json(
                {"event": "mark", "streamSid": session.stream_sid, "mark": {"name": mark_name}}
            )
            await asyncio.wait_for(completion.wait(), timeout=self._playback_timeout)
        finally:
            session.mark_events.pop(mark_name, None)
            chunks.clear()

    async def resume_after_secure_field(self, task_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None:
            raise RuntimeError("The active call is no longer connected.")
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        session.expected_field = None
        session.secure_mode = False
        await session.realtime.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "The user supplied one protected field through local voice. Say only: "
                            "'Thanks. Please continue.' Do not identify or repeat the field or its value."
                        )
                    },
                }
            )
        )
        self._events.append("secure_mode.exited", {"task_id": task_id})

    async def resume_from_takeover(self, task_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None:
            raise RuntimeError("The active call is no longer connected.")
        expected_field = session.expected_field
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        session.expected_field = None
        session.secure_mode = False
        try:
            await session.realtime.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "instructions": (
                                "The user has handed the active call back to you after handling a protected exchange "
                                "personally. Continue naturally from here without identifying or repeating any "
                                "protected information."
                            )
                        },
                    }
                )
            )
        except Exception:
            session.expected_field = expected_field
            session.secure_mode = True
            raise
        self._events.append("secure_mode.takeover_resumed", {"task_id": task_id})

    async def _enter_secure_mode(self, task_id: str, session: ActiveRealtimeSession, field_name: str) -> None:
        session.secure_mode = True
        session.expected_field = field_name
        await session.twilio.send_json({"event": "clear", "streamSid": session.stream_sid})
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        state = self._secure_requester(task_id, field_name) if self._secure_requester else {}
        self._events.append(
            "secure_mode.takeover_required" if state.get("call_state") == "HUMAN_TAKEOVER" else "secure_mode.entered",
            {"task_id": task_id, "field": field_name},
        )

    async def bridge(
        self,
        twilio: WebSocket,
        expected_task_id: str = "",
        expected_queue_index: int | None = None,
        expected_call_sid: str = "",
    ) -> None:
        realtime = None
        await twilio.accept()
        try:
            start = await self._receive_start(twilio)
        except (WebSocketDisconnect, ValueError) as error:
            self._events.append(
                "media.start_failed",
                {
                    "reason": type(error).__name__,
                    "detail": str(error),
                    "expected_task_id": expected_task_id,
                    "expected_queue_index": expected_queue_index,
                    "expected_call_sid": expected_call_sid,
                },
            )
            with suppress(RuntimeError):
                await twilio.close(code=1008)
            return
        task_id = ""
        queue_index_raw = ""
        call_sid = ""
        failure_check = "start_payload"
        try:
            stream_sid = str(start["streamSid"])
            parameters = start["start"].get("customParameters", {})
            task_id = str(parameters.get("task_id", ""))
            queue_index_raw = str(parameters.get("queue_index", ""))
            call_sid = str(start["start"].get("callSid", ""))
            failure_check = "queue_index_format"
            queue_index = int(queue_index_raw)
            failure_check = "task_id"
            if expected_task_id and task_id != expected_task_id:
                raise ValueError("Media task identity does not match the approved call.")
            failure_check = "queue_index"
            if expected_queue_index is not None and queue_index != expected_queue_index:
                raise ValueError("Media queue identity does not match the approved call.")
            failure_check = "call_sid"
            if expected_call_sid and call_sid != expected_call_sid:
                raise ValueError("Media call identity does not match the approved call.")
            failure_check = "context_reader"
            context = self._context_reader(task_id, queue_index)
        except Exception as error:
            self._events.append(
                "media.identity_rejected",
                {
                    "check": failure_check,
                    "reason": type(error).__name__,
                    "expected_task_id": expected_task_id,
                    "received_task_id": task_id,
                    "expected_queue_index": expected_queue_index,
                    "received_queue_index": queue_index_raw,
                    "expected_call_sid": expected_call_sid,
                    "received_call_sid": call_sid,
                },
            )
            await twilio.close(code=1008)
            return
        credentials = self._credentials()
        model = self._realtime_model()
        url = f"wss://api.openai.com/v1/realtime?model={model}"
        try:
            async with self._connector(
                url,
                additional_headers={"Authorization": f"Bearer {credentials.openai_api_key}"},
            ) as realtime:
                session = ActiveRealtimeSession(realtime=realtime, twilio=twilio, stream_sid=stream_sid)
                session.context = context
                session.debug_trace = CallDebugTrace(task_id, call_sid)
                async with self._lock:
                    self._sessions[task_id] = session
                if self._call_connected:
                    self._call_connected(task_id)
                session_update = realtime_session_update(context, self._transcription_model())
                opening = initial_response(context)
                session.debug_trace.append(
                    "speaker.context",
                    {"context": context, "session_update": session_update, "initial_response": opening},
                )
                await realtime.send(json.dumps(session_update))
                await realtime.send(json.dumps(opening))
                self._events.append("realtime.connected", {"task_id": task_id, "model": model})
                to_openai = asyncio.create_task(self._twilio_to_openai(session))
                to_twilio = asyncio.create_task(self._openai_to_twilio(session, task_id))
                done, pending = await asyncio.wait({to_openai, to_twilio}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exception = task.exception()
                    if exception and not isinstance(exception, WebSocketDisconnect):
                        raise exception
        except WebSocketDisconnect:
            pass
        except Exception as error:
            self._events.append("realtime.failed", {"task_id": task_id, "reason": type(error).__name__})
            with suppress(RuntimeError):
                await twilio.close(code=1011)
        finally:
            async with self._lock:
                active = self._sessions.get(task_id)
                if active is not None and active.realtime is realtime:
                    self._sessions.pop(task_id, None)
            self._events.append("realtime.disconnected", {"task_id": task_id})

    async def _receive_start(self, twilio: WebSocket) -> dict[str, Any]:
        while True:
            message = await twilio.receive_json()
            if message.get("event") == "start":
                return message
            if message.get("event") == "stop":
                raise ValueError("Stream stopped before it started.")

    async def _twilio_to_openai(self, session: ActiveRealtimeSession) -> None:
        while True:
            message = await session.twilio.receive_json()
            event = message.get("event")
            if event == "media":
                if not session.secure_mode and not session.waiting_for_user:
                    await session.realtime.send(
                        json.dumps({"type": "input_audio_buffer.append", "audio": message["media"]["payload"]})
                    )
            elif event == "mark":
                mark = session.mark_events.get(message.get("mark", {}).get("name", ""))
                if mark:
                    mark.set()
            elif event == "stop":
                return

    async def _openai_to_twilio(self, session: ActiveRealtimeSession, task_id: str) -> None:
        async for raw in session.realtime:
            event = json.loads(raw)
            event_type = event.get("type")
            if event_type == "response.output_audio.delta":
                if not session.secure_mode and (not session.waiting_for_user or session.hold_response_active):
                    await session.twilio.send_json(
                        {
                            "event": "media",
                            "streamSid": session.stream_sid,
                            "media": {"payload": event["delta"]},
                        }
                    )
            elif (
                event_type == "input_audio_buffer.speech_started"
                and not session.secure_mode
                and not session.waiting_for_user
            ):
                await session.twilio.send_json({"event": "clear", "streamSid": session.stream_sid})
            if event_type == "response.function_call_arguments.done":
                await self._handle_function_call(session, task_id, event)
            if event_type == "response.done" and session.hold_response_active:
                session.hold_response_active = False
                session.hold_complete.set()
            transcript = transcript_from_realtime_event(event)
            if transcript and transcript[1].strip() and not session.secure_mode:
                sensitive_field = requested_sensitive_field(transcript[1])
                if transcript[0] == "representative" and sensitive_field:
                    await self._enter_secure_mode(task_id, session, sensitive_field)
                else:
                    self._transcript_writer(task_id, transcript[0], transcript[1])
                    if transcript[0] == "representative" and not session.waiting_for_user:
                        await self._gate_representative_turn(session, task_id, transcript[1])
            if event_type == "error":
                detail = event.get("error", {})
                self._events.append(
                    "realtime.protocol_error",
                    {"task_id": task_id, "type": detail.get("type"), "code": detail.get("code")},
                )

    async def _gate_representative_turn(
        self,
        session: ActiveRealtimeSession,
        task_id: str,
        utterance: str,
    ) -> None:
        request = gatekeeper_request(utterance, session.context, session.updates_from_user)
        session.debug_trace and session.debug_trace.append("gatekeeper.request", gatekeeper_debug_payload(request))
        started = monotonic()
        try:
            verdict = await self._gatekeeper.classify(request)
        except Exception as error:
            latency_ms = round((monotonic() - started) * 1000)
            self._events.append(
                "gatekeeper.failed",
                {"task_id": task_id, "reason": type(error).__name__, "latency_ms": latency_ms},
            )
            verdict = GatekeeperVerdict(
                verdict="unanswerable",
                question=f'The representative said: "{utterance}" What should Relay say?',
            )
        else:
            latency_ms = round((monotonic() - started) * 1000)
        session.debug_trace and session.debug_trace.append(
            "gatekeeper.verdict",
            {"verdict": verdict.model_dump(), "latency_ms": latency_ms},
        )
        self._events.append(
            "gatekeeper.verdict",
            {"task_id": task_id, "verdict": verdict.verdict, "latency_ms": latency_ms},
        )
        if verdict.verdict == "answerable":
            await session.realtime.send(json.dumps({"type": "response.create"}))
            return
        question = verdict.question.strip() or f'How should Relay answer: "{utterance}"?'
        session.waiting_for_user = True
        session.pending_tool_call_id = None
        session.hold_response_active = True
        session.hold_complete.clear()
        if self._user_input_requester is not None:
            self._user_input_requester(task_id, question, "text", True)
        await session.realtime.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "Say exactly one brief natural hold line to the representative, such as "
                            "'Let me check on that one second.' Do not answer the question, ask another question, "
                            "restart the introduction, or call a tool."
                        )
                    },
                }
            )
        )
        self._events.append("realtime.user_input_requested", {"task_id": task_id, "source": "gatekeeper"})

    async def _handle_function_call(
        self,
        session: ActiveRealtimeSession,
        task_id: str,
        event: dict[str, Any],
    ) -> None:
        if event.get("name") != "request_user_input":
            return
        call_id = str(event.get("call_id", "")).strip()
        try:
            arguments = json.loads(event.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        question = str(arguments.get("question", "")).strip()
        input_kind = str(arguments.get("input_kind", ""))
        blocking = arguments.get("blocking")
        if not call_id or not question or input_kind != "text" or not isinstance(blocking, bool):
            self._events.append(
                "realtime.tool_rejected",
                {"task_id": task_id, "tool": "request_user_input", "reason": "invalid_arguments"},
            )
            return
        if session.waiting_for_user:
            return
        if self._user_input_requester is None:
            self._events.append(
                "realtime.tool_rejected",
                {"task_id": task_id, "tool": "request_user_input", "reason": "handler_unavailable"},
            )
            return
        self._user_input_requester(task_id, question, input_kind, blocking)
        session.waiting_for_user = True
        session.pending_tool_call_id = call_id
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        self._events.append("realtime.user_input_requested", {"task_id": task_id, "input_kind": input_kind})


def gatekeeper_debug_payload(request: GatekeeperRequest) -> dict[str, Any]:
    return {
        "instructions": request.instructions,
        "latest_utterance": request.latest_utterance,
        "context": request.context,
        "updates_from_user": list(request.updates_from_user),
        "messages": request.messages(),
    }
