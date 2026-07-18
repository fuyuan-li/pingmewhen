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
    AuthorityVeto,
    Gatekeeper,
    GatekeeperRequest,
    GatekeeperVerdict,
    PrivateMessageRequest,
    gatekeeper_request,
)
from relay_agent.local_tts import LocalTTSRenderer, MacOSLocalTTS
from relay_agent.names import normalize_display_name


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

TRIVIAL_ACKNOWLEDGEMENTS = {
    "ah",
    "alright",
    "all right",
    "got it",
    "i see",
    "mhm",
    "mmhm",
    "mm hmm",
    "mm-hmm",
    "okay",
    "ok",
    "right",
    "sounds good",
    "sure",
    "uh huh",
    "uh-huh",
    "understood",
    "yeah",
    "yep",
    "yes",
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


def is_trivial_acknowledgement(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s'-]", "", text.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return "?" not in text and cleaned in TRIVIAL_ACKNOWLEDGEMENTS


def gatekeeper_context(context: dict[str, Any]) -> dict[str, Any]:
    action = context.get("action", {})
    return {
        "approved_call": {
            "purpose": action.get("purpose", ""),
            "target": action.get("target", ""),
            "known_facts": action.get("known_facts", []),
        },
        "document_context": context.get("document_context", ""),
    }


def gatekeeper_identity(context: dict[str, Any]) -> tuple[str, str]:
    represented_user = normalize_display_name(str(context.get("caller_name", ""))) or "the represented user"
    representative_name = str(context.get("action", {}).get("target", "")).strip() or "the representative"
    return represented_user, representative_name


def speaker_addressing_preamble(context: dict[str, Any]) -> str:
    """Identity anchor prepended to every backend-driven response whose instructions field would otherwise
    replace the session instructions and leave the Speaker with no sense of who it is or who it is speaking to."""
    caller_name = normalize_display_name(str(context.get("caller_name", ""))) or "the user"
    target = str(context.get("action", {}).get("target", "")).strip() or "the representative"
    return (
        "WHO YOU ARE ON THIS REPLY: You are Relay, an AI phone caller, speaking OUT LOUD on a live call to "
        f"{target}. You represent {caller_name} and are calling on their behalf. Speak TO {target}, addressing "
        f"them directly as 'you'. Refer to the person you represent as {caller_name} by name; never call "
        f"{caller_name} 'they', 'them', 'the customer', 'the client', or 'the user', and never talk about "
        f"{caller_name} in the third person. You are not {caller_name} and you are not {target}: you speak for "
        f"{caller_name} to {target}. You already introduced yourself earlier in this call — do not reintroduce "
        "yourself or restate that you are an AI unless asked. Relay the information in the correct direction: keep "
        "who is asking, offering, conceding, or requesting exactly as it actually is, and never swap it. You are "
        f"the one telling {target} what {caller_name} has decided — say it as your own statement to {target}, for "
        f"example 'I can confirm {caller_name} is okay going ahead at $100 a month', never 'you can let "
        f"{caller_name} know' (you are not asking {target} to pass a message to {caller_name}). "
    )


@dataclass
class ActiveRealtimeSession:
    realtime: Any
    twilio: WebSocket
    stream_sid: str
    task_id: str = ""
    secure_mode: bool = False
    waiting_for_user: bool = False
    pending_question: str = ""
    pending_interaction_id: str = ""
    pending_interaction_reason: str = ""
    expected_field: str | None = None
    mark_events: dict[str, asyncio.Event] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    context_updates: list[dict[str, Any]] = field(default_factory=list)
    hold_response_active: bool = False
    hold_complete: asyncio.Event = field(default_factory=asyncio.Event)
    debug_trace: CallDebugTrace | None = None
    context_item_ack: asyncio.Event = field(default_factory=asyncio.Event)
    pending_context_item_id: str = ""
    response_pending: bool = False
    response_request_id: str = ""
    response_server_id: str = ""
    response_purpose: str = ""
    response_complete: asyncio.Event = field(default_factory=asyncio.Event)
    response_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    deferred_representative_turns: list[str] = field(default_factory=list)
    keepalive_response_active: bool = False
    keepalive_complete: asyncio.Event = field(default_factory=asyncio.Event)
    keepalive_task: asyncio.Task | None = None
    has_disclosed: bool = False


@dataclass(frozen=True)
class PrivateMessageDelivery:
    disposition: str
    context_update: dict[str, Any] | None = None
    private_reply: str = ""
    resumed_call: bool = False
    interaction_id: str = ""


def build_speaker_instructions(
    context: dict[str, Any],
    context_updates: list[dict[str, Any]] | None = None,
    has_disclosed: bool = False,
) -> str:
    action = context.get("action", {})
    caller_name = normalize_display_name(str(context.get("caller_name", ""))) or "the user"
    caller_name_literal = json.dumps(caller_name)
    target_literal = json.dumps(action.get("target", "the representative"))
    private_context = "\n".join(context.get("private_messages", [])[-20:])
    document_context = context.get("document_context", "")
    prior_calls = context.get("prior_call_transcript", "")
    confirmed_updates = json.dumps(context_updates or context.get("context_updates", []), ensure_ascii=False)
    purpose = action.get("purpose", "the approved purpose")
    goal = context.get("goal", "")
    if has_disclosed:
        turn_section = (
            "CONTINUING: You have ALREADY introduced yourself and disclosed that you are an AI earlier in this "
            "call. Do NOT introduce yourself again, do NOT repeat the AI disclosure, and do NOT restate the "
            "call's purpose from the top. Simply continue the conversation, responding directly and briefly to "
            "what the representative just said. Never deny being an AI if asked.\n\n"
        )
    else:
        turn_section = (
            "FIRST TURN: You never speak first. This is an outbound call you placed, so wait for the "
            "representative to speak — a greeting, 'hello,' or asking who is calling — before saying anything. If "
            "instead you hear hold music, an automated queue message, or silence, stay silent and keep waiting; "
            "never fill the silence, greet first, or ask if anyone is there. Once the representative actually "
            "speaks, your first reply must open with a short AI disclosure and nothing else before it, then "
            "briefly state ONLY the primary service or topic you are calling about (see GOAL below) in the same "
            "reply — for example 'setting up internet service at his address.' Do NOT announce your negotiation "
            "strategy, a target or maximum price, discounts you are hoping for, or your full multi-step plan in "
            "the opening; those come out later only as the conversation needs them. Never leave the representative "
            "wondering why you called or invite generic small talk. Never repeat the disclosure later in the "
            "call, and never restart it if interrupted — continue from what the representative said. Never deny "
            "being an AI if asked, and never deny or forget that you placed this call for a specific reason.\n\n"
        )
    return (
        "IDENTITY — read this first, keep these two facts separate for the whole call:\n"
        f"- You represent (you are calling ON BEHALF OF): {caller_name_literal}\n"
        f"- You are calling (the organization/person you dial and speak to): {target_literal}\n"
        f"- Never say you are calling {caller_name_literal}. That is who you represent, not who you are calling.\n"
        f"- Never say you are {caller_name_literal} or claim to be them. You represent them; you are not them.\n\n"
        f"NAME USAGE: Always refer to the represented person by the exact name {caller_name_literal}. Never replace "
        f"{caller_name_literal} with 'the customer,' 'the user,' 'the client,' 'the person,' or pronouns such as "
        "they, them, their, he, she, him, or her. Repeat the name when grammar requires a reference.\n\n"
        f"ADDRESSING: You are speaking directly to {target_literal} on the phone. Address {target_literal} as "
        f"'you'. Never refer to {target_literal} in the third person as 'they' or 'them' while speaking to them. "
        f"When you relay something {caller_name_literal} wants, say it in the correct direction — for example, "
        f"'{caller_name} is hoping you can lower the price', not 'they are hoping they can lower the price'.\n\n"
        "ROLE: You are Relay, the outbound caller who initiated this call to accomplish a specific approved goal. "
        "You are not an inbound support agent: never welcome the other person to a support service, ask what they "
        "called about, or offer generic help. Your spoken audio is always addressed to the representative you "
        "called. Private text comes from the person you represent; it is an answer or instruction for you, not "
        "speech from the representative. Never acknowledge or answer that private person aloud as though they were "
        "on the phone. Reformulate their information naturally for the representative.\n\n"
        + turn_section
        + "GOAL: Pursue only this approved purpose: "
        f"{purpose}. The overall user goal is: {goal}. Continue each turn naturally from the "
        "immediately preceding conversation.\n\n"
        "TURN CONTROL: The backend Gatekeeper is the sole authority that decides whether enough information exists "
        "to answer each representative turn. The backend creates your response only after approval. Never invent a "
        "missing fact or independently address the private user.\n\n"
        "SPEAKING STYLE: Keep every turn to roughly one or two short sentences. Do not front-load every detail or goal "
        "into one long statement. Make one useful point, then pause and let the representative respond. Prefer a "
        "natural back-and-forth phone rhythm over a complete or exhaustive statement. Never vocalize planning, "
        "analysis, self-talk, rehearsal, or commentary about how you will answer; output only words intended for the "
        "representative.\n\n"
        "BOUNDARIES: Never claim to be the user. Do not provide payment-card data or a full Social Security number. "
        "Never choose, accept, reject, counter, approve, schedule, enroll, purchase, cancel, or otherwise commit on "
        "behalf of the represented person unless the newest confirmed backend context explicitly records that exact "
        "decision for the current pending interaction. A budget, preference, or overall goal is not approval. "
        "Never read phone numbers, account numbers, or other reference identifiers aloud unless the representative "
        "explicitly asks for that specific detail; identifiers in known facts are internal reference by default. Do "
        "not choose a regulated product for the user. Be concise and natural.\n\n"
        "CONTEXT — the user's private task messages are:\n"
        f"{private_context}\nRelevant local document text is:\n{document_context}\nPrior separate call transcript for task "
        f"memory only:\n{prior_calls}\nTreat this as a new representative who has not heard any prior call.\n"
        "CONFIRMED PRIVATE CONTEXT UPDATES — these records were validated by the backend Gatekeeper. They are facts "
        "or directions from the person you represent, never statements made by the representative:\n"
        f"{confirmed_updates}"
    )


def realtime_session_update(
    context: dict[str, Any],
    transcription_model: str = "gpt-4o-mini-transcribe",
    context_updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": build_speaker_instructions(context, context_updates, has_disclosed=False),
            "output_modalities": ["audio"],
            "tools": [],
            "tool_choice": "none",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": transcription_model, "language": "en"},
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": True,
                        "silence_duration_ms": 900,
                    },
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": "marin"},
            },
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
        user_input_requester: Callable[..., dict[str, Any]] | None = None,
        call_connected: Callable[[str], dict[str, Any]] | None = None,
        tts_renderer: LocalTTSRenderer | None = None,
        playback_timeout: float = 20,
        realtime_model: Callable[[], str] | None = None,
        transcription_model: Callable[[], str] | None = None,
        session_update_timeout: float = 0,
        response_delivery_timeout: float = 0,
        waiting_keepalive_interval: float = 10,
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
        self._session_update_timeout = session_update_timeout
        self._response_delivery_timeout = response_delivery_timeout
        self._waiting_keepalive_interval = waiting_keepalive_interval
        self._sessions: dict[str, ActiveRealtimeSession] = {}
        self._lock = asyncio.Lock()

    async def _send_response_create(
        self,
        session: ActiveRealtimeSession,
        request: dict[str, Any] | None = None,
        wait_for_available: bool = False,
        purpose: str = "unspecified",
    ) -> bool:
        request = request or {"type": "response.create"}
        while True:
            async with session.response_lock:
                if not session.response_pending:
                    request_id = uuid4().hex
                    session.response_pending = True
                    session.response_request_id = request_id
                    session.response_server_id = ""
                    session.response_purpose = purpose
                    session.response_complete.clear()
                    try:
                        await session.realtime.send(json.dumps(request))
                    except Exception as error:
                        session.response_pending = False
                        session.response_request_id = ""
                        session.response_purpose = ""
                        session.response_complete.set()
                        self._events.append(
                            "realtime.response_send_failed",
                            {
                                "task_id": session.task_id,
                                "purpose": purpose,
                                "request_id": request_id,
                                "reason": type(error).__name__,
                            },
                        )
                        raise
                    self._events.append(
                        "realtime.response_requested",
                        {"task_id": session.task_id, "purpose": purpose, "request_id": request_id},
                    )
                    if session.debug_trace:
                        session.debug_trace.append(
                            "speaker.response_requested",
                            {"purpose": purpose, "request_id": request_id},
                        )
                    return True
                completion = session.response_complete
            if not wait_for_available:
                return False
            await completion.wait()

    async def inject(self, task_id: str, text: str) -> PrivateMessageDelivery | None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None or session.secure_mode:
            return None
        session.task_id = session.task_id or task_id
        waiting_for_user = session.waiting_for_user
        interaction_id = session.pending_interaction_id if waiting_for_user else ""
        # Stop the keep-alive timer immediately, before the (~1-2s) classification call below, so a
        # scheduled keep-alive can never race with the response that actually delivers this answer.
        await self._stop_waiting_keepalive(session)
        represented_user, representative_name = gatekeeper_identity(session.context)
        request = PrivateMessageRequest(
            text=text.strip(),
            context=session.context,
            context_updates=tuple(session.context_updates),
            waiting_for_user=waiting_for_user,
            pending_question=session.pending_question,
            pending_reason=session.pending_interaction_reason,
            pending_interaction_id=interaction_id,
            represented_user=represented_user,
            representative_name=representative_name,
        )
        session.debug_trace and session.debug_trace.append(
            "gatekeeper.private_message_request",
            {"request": request.messages(), "raw_text": text},
        )
        route = await self._gatekeeper.route_private_message(request)
        session.debug_trace and session.debug_trace.append(
            "gatekeeper.private_message_route", {"route": route.model_dump()}
        )
        if route.disposition == "private_meta":
            self._events.append("realtime.private_message_kept_private", {"task_id": task_id})
            if waiting_for_user:
                self._start_waiting_keepalive(session, task_id)
            return PrivateMessageDelivery(
                disposition=route.disposition,
                private_reply=route.private_reply.strip(),
            )
        if session.hold_response_active and waiting_for_user:
            try:
                await asyncio.wait_for(session.hold_complete.wait(), timeout=3)
            except TimeoutError:
                await session.realtime.send(json.dumps({"type": "response.cancel"}))
            session.hold_response_active = False
            session.hold_complete.set()
        if session.keepalive_response_active:
            await session.realtime.send(json.dumps({"type": "response.cancel"}))
            session.keepalive_response_active = False
            session.keepalive_complete.set()
        update = route.speaker_update.model_dump() if route.speaker_update else None
        if update is None:
            if waiting_for_user:
                self._start_waiting_keepalive(session, task_id)
            raise RuntimeError("Gatekeeper did not provide a Speaker update.")
        update_record = {"id": uuid4().hex, **update}
        if interaction_id:
            update_record["interaction_id"] = interaction_id
        session.context_updates.append(update_record)
        item_id = uuid4().hex
        context_item = {
            "type": "conversation.item.create",
            "item": {
                "id": item_id,
                "type": "message",
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "CONFIRMED CONTEXT UPDATE FROM RELAY BACKEND\n"
                            f"Interaction: {update_record.get('interaction_id', 'none')}\n"
                            f"Key: {update_record['key']}\n"
                            f"Value: {update_record['value']}\n"
                            f"Meaning: {update_record['summary']}\n"
                            "This is information from the person you represent, not speech from the representative."
                        ),
                    }
                ],
            },
        }
        session.pending_context_item_id = item_id
        session.context_item_ack.clear()
        await session.realtime.send(json.dumps(context_item))
        if self._session_update_timeout > 0:
            try:
                await asyncio.wait_for(session.context_item_ack.wait(), timeout=self._session_update_timeout)
            except TimeoutError:
                session.context_updates.pop()
                if waiting_for_user:
                    self._start_waiting_keepalive(session, task_id)
                raise RuntimeError("Speaker did not acknowledge the confirmed context update.")
            finally:
                session.pending_context_item_id = ""
        preamble = speaker_addressing_preamble(session.context)
        response_request = {
            "type": "response.create",
            "response": {
                "instructions": (
                    preamble
                    + (
                        "The backend appended a confirmed context update after consulting the person you represent. "
                        "Use the newest CONFIRMED CONTEXT UPDATE conversation item. Convey it to the representative "
                        "as a natural continuation of the call — if it answers their question, answer them; if it is "
                        "a question or request from the person you represent, ask it of the representative in the "
                        "correct direction. Do not acknowledge the private exchange with phrases such as 'Got it,' "
                        "and do not address the represented person aloud."
                        if waiting_for_user
                        else (
                            "The backend appended a confirmed context update or call direction. Use the newest "
                            "CONFIRMED CONTEXT UPDATE conversation item naturally while speaking only to the "
                            "representative. Do not mention a private channel or acknowledge the represented person "
                            "aloud."
                        )
                    )
                )
            },
        }
        if session.debug_trace:
            session.debug_trace.append(
                "speaker.private_injection",
                {
                    "waiting_for_user": waiting_for_user,
                    "context_item": context_item,
                    "response_create": response_request,
                },
            )
        # Clear waiting_for_user before the response streams, not after it completes: the audio-forwarding gate in
        # _openai_to_twilio checks this flag on every output_audio.delta as the answer is spoken, so leaving it set
        # until response.done would silently drop the entire spoken answer even though it plays out normally.
        if waiting_for_user:
            session.waiting_for_user = False
            session.pending_question = ""
            session.pending_interaction_id = ""
            session.pending_interaction_reason = ""
        try:
            await self._send_response_create(
                session,
                response_request,
                wait_for_available=True,
                purpose="private_answer" if waiting_for_user else "private_instruction",
            )
            if waiting_for_user and self._response_delivery_timeout > 0:
                await asyncio.wait_for(
                    session.response_complete.wait(),
                    timeout=self._response_delivery_timeout,
                )
        except Exception as error:
            if isinstance(error, TimeoutError):
                with suppress(Exception):
                    await session.realtime.send(json.dumps({"type": "response.cancel"}))
            if waiting_for_user:
                session.waiting_for_user = True
                session.pending_question = request.pending_question
                session.pending_interaction_id = interaction_id
                session.pending_interaction_reason = request.pending_reason
                self._start_waiting_keepalive(session, task_id)
            if isinstance(error, TimeoutError):
                raise RuntimeError("Speaker did not complete the confirmed answer in time.") from error
            raise
        self._events.append("realtime.instruction_injected", {"task_id": task_id})
        return PrivateMessageDelivery(
            disposition=route.disposition,
            context_update=update_record,
            private_reply=route.private_reply.strip(),
            resumed_call=waiting_for_user,
            interaction_id=interaction_id,
        )

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
        await self._send_response_create(
            session,
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "The user supplied one protected field through local voice. Say only: "
                        "'Thanks. Please continue.' Do not identify or repeat the field or its value."
                    )
                },
            },
            wait_for_available=True,
            purpose="secure_field_resume",
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
            await self._send_response_create(
                session,
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            speaker_addressing_preamble(session.context)
                            + "The user has handed the active call back to you after handling a protected exchange "
                            "personally. Continue naturally from here without identifying or repeating any "
                            "protected information."
                        )
                    },
                },
                wait_for_available=True,
                purpose="takeover_resume",
            )
        except Exception:
            session.expected_field = expected_field
            session.secure_mode = True
            raise
        self._events.append("secure_mode.takeover_resumed", {"task_id": task_id})

    async def _enter_secure_mode(self, task_id: str, session: ActiveRealtimeSession, field_name: str) -> None:
        await self._stop_waiting_keepalive(session)
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
                session = ActiveRealtimeSession(
                    realtime=realtime,
                    twilio=twilio,
                    stream_sid=stream_sid,
                    task_id=task_id,
                )
                session.context = context
                session.context_updates = list(context.get("context_updates", []))
                session.debug_trace = CallDebugTrace(task_id, call_sid)
                async with self._lock:
                    self._sessions[task_id] = session
                if self._call_connected:
                    self._call_connected(task_id)
                session_update = realtime_session_update(
                    context, self._transcription_model(), session.context_updates
                )
                session.debug_trace.append(
                    "speaker.context",
                    {"context": context, "session_update": session_update},
                )
                await realtime.send(json.dumps(session_update))
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
            if task_id:
                active_session = self._sessions.get(task_id)
                if active_session is not None:
                    await self._stop_waiting_keepalive(active_session)
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
                if not session.secure_mode:
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
            if event_type == "response.created":
                response = event.get("response", {})
                session.response_server_id = str(response.get("id", ""))
                payload = {
                    "task_id": task_id,
                    "purpose": session.response_purpose,
                    "request_id": session.response_request_id,
                    "response_id": session.response_server_id,
                }
                self._events.append("realtime.response_created", payload)
                session.debug_trace and session.debug_trace.append("speaker.response_created", payload)
            if event_type in {"conversation.item.created", "conversation.item.added"}:
                item = event.get("item", {})
                if str(item.get("id", "")) == session.pending_context_item_id:
                    session.context_item_ack.set()
            if event_type == "response.output_audio.delta":
                if not session.secure_mode and (
                    not session.waiting_for_user
                    or session.hold_response_active
                    or session.keepalive_response_active
                ):
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
            if event_type == "response.done":
                response = event.get("response", {})
                completed_response_id = str(response.get("id", "")) or session.response_server_id
                payload = {
                    "task_id": task_id,
                    "purpose": session.response_purpose,
                    "request_id": session.response_request_id,
                    "response_id": completed_response_id,
                    "status": str(response.get("status", "")),
                }
                self._events.append("realtime.response_completed", payload)
                session.debug_trace and session.debug_trace.append("speaker.response_completed", payload)
                session.response_pending = False
                session.response_request_id = ""
                session.response_server_id = ""
                session.response_purpose = ""
                session.response_complete.set()
                if session.hold_response_active:
                    session.hold_response_active = False
                    session.hold_complete.set()
                if session.keepalive_response_active:
                    session.keepalive_response_active = False
                    session.keepalive_complete.set()
                if session.deferred_representative_turns and not session.waiting_for_user:
                    utterance = session.deferred_representative_turns.pop(0)
                    await self._gate_representative_turn(session, task_id, utterance)
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
                session.debug_trace and session.debug_trace.append("speaker.protocol_error", {"error": detail})
                self._events.append(
                    "realtime.protocol_error",
                    {
                        "task_id": task_id,
                        "type": detail.get("type"),
                        "code": detail.get("code"),
                        "param": detail.get("param"),
                    },
                )

    def _representative_turn_request(self, session: ActiveRealtimeSession) -> dict[str, Any] | None:
        # response.instructions replaces the session instructions for this response, so we must pass the COMPLETE
        # speaker instructions (identity, goal, boundaries, private context) every time — never a fragment, which
        # would wipe the model's context. The only per-turn difference is the disclosure toggle: the first turn
        # still introduces and discloses; every later turn is told it has already done so and must not repeat it.
        disclosed_before = session.has_disclosed
        session.has_disclosed = True
        return {
            "type": "response.create",
            "response": {
                "instructions": build_speaker_instructions(
                    session.context, session.context_updates, has_disclosed=disclosed_before
                )
            },
        }

    async def _gate_representative_turn(
        self,
        session: ActiveRealtimeSession,
        task_id: str,
        utterance: str,
    ) -> None:
        session.task_id = session.task_id or task_id
        if session.response_pending:
            session.deferred_representative_turns.append(utterance)
            self._events.append("realtime.representative_turn_deferred", {"task_id": task_id})
            return
        if not session.has_disclosed:
            # The representative's first-ever utterance on the call cannot require user authority: no terms,
            # offers, or decisions have been exchanged yet, whatever they open with (a bare greeting or a long
            # self-introduction). Bypass Gatekeeper on turn position, not on guessing the utterance's content.
            self._events.append(
                "gatekeeper.bypassed",
                {"task_id": task_id, "reason": "first_turn"},
            )
            await self._send_response_create(
                session, self._representative_turn_request(session), purpose="representative_turn"
            )
            return
        if is_trivial_acknowledgement(utterance):
            self._events.append(
                "gatekeeper.bypassed",
                {"task_id": task_id, "reason": "trivial_acknowledgement"},
            )
            await self._send_response_create(
                session, self._representative_turn_request(session), purpose="representative_turn"
            )
            return
        represented_user, representative_name = gatekeeper_identity(session.context)
        request = gatekeeper_request(
            utterance,
            gatekeeper_context(session.context),
            session.context_updates,
            represented_user,
            representative_name,
        )
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
                route="consult_user",
                reason="uncertainty",
                representative_update=utterance,
                question_to_user="How should Relay respond?",
            )
        else:
            latency_ms = round((monotonic() - started) * 1000)
        session.debug_trace and session.debug_trace.append(
            "gatekeeper.verdict",
            {"verdict": verdict.model_dump(), "latency_ms": latency_ms},
        )
        self._events.append(
            "gatekeeper.verdict",
            {
                "task_id": task_id,
                "route": verdict.route,
                "reason": verdict.reason,
                "latency_ms": latency_ms,
            },
        )
        if verdict.route == "continue":
            veto_started = monotonic()
            try:
                veto = await self._gatekeeper.veto(request)
            except Exception as error:
                veto_latency_ms = round((monotonic() - veto_started) * 1000)
                self._events.append(
                    "gatekeeper.veto_failed",
                    {"task_id": task_id, "reason": type(error).__name__, "latency_ms": veto_latency_ms},
                )
                veto = AuthorityVeto(
                    requires_user=True,
                    reason="uncertainty",
                    representative_update=utterance,
                    question_to_user="How should Relay respond?",
                )
            else:
                veto_latency_ms = round((monotonic() - veto_started) * 1000)
            session.debug_trace and session.debug_trace.append(
                "gatekeeper.authority_veto",
                {"verdict": veto.model_dump(), "latency_ms": veto_latency_ms},
            )
            self._events.append(
                "gatekeeper.authority_veto",
                {
                    "task_id": task_id,
                    "requires_user": veto.requires_user,
                    "reason": veto.reason,
                    "latency_ms": veto_latency_ms,
                },
            )
            if not veto.requires_user:
                await self._send_response_create(
                    session, self._representative_turn_request(session), purpose="representative_turn"
                )
                return
            verdict = GatekeeperVerdict(
                route="consult_user",
                reason=veto.reason,
                representative_update=veto.representative_update,
                question_to_user=veto.question_to_user,
            )
        question = f"{verdict.representative_update.strip()}\n\n{verdict.question_to_user.strip()}"
        interaction_id = uuid4().hex
        session.waiting_for_user = True
        session.pending_question = question
        session.pending_interaction_id = interaction_id
        session.pending_interaction_reason = verdict.reason
        session.hold_response_active = True
        session.hold_complete.clear()
        if self._user_input_requester is not None:
            self._user_input_requester(
                task_id,
                question,
                "text",
                True,
                interaction_id,
                verdict.reason,
                verdict.representative_update.strip(),
            )
        await self._send_response_create(
            session,
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Say exactly one brief natural hold line to the representative, such as "
                        "'Let me check on that one second.' Produce that single short line, then end your turn "
                        "immediately. Do not say it a second time, do not restate it in other words, and do not add "
                        "any second sentence. Do not answer the question, state any price, number, address, or "
                        "decision, ask another question, restart the introduction, or call a tool."
                    ),
                },
            },
            purpose="hold",
        )
        self._start_waiting_keepalive(session, task_id)
        self._events.append(
            "realtime.user_input_requested",
            {
                "task_id": task_id,
                "source": "gatekeeper",
                "interaction_id": interaction_id,
                "reason": verdict.reason,
            },
        )

    def _start_waiting_keepalive(self, session: ActiveRealtimeSession, task_id: str) -> None:
        if session.keepalive_task is not None and not session.keepalive_task.done():
            session.keepalive_task.cancel()
        session.keepalive_task = asyncio.create_task(self._waiting_keepalive(session, task_id))

    async def _stop_waiting_keepalive(self, session: ActiveRealtimeSession) -> None:
        task = session.keepalive_task
        session.keepalive_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _waiting_keepalive(self, session: ActiveRealtimeSession, task_id: str) -> None:
        try:
            await session.hold_complete.wait()
            while session.waiting_for_user and not session.secure_mode:
                await asyncio.sleep(self._waiting_keepalive_interval)
                if not session.waiting_for_user or session.secure_mode:
                    return
                session.keepalive_response_active = True
                session.keepalive_complete.clear()
                await self._send_response_create(
                    session,
                    {
                        "type": "response.create",
                        "response": {
                            "instructions": (
                                "Say exactly one short natural keep-alive line, such as 'Just need one more "
                                "moment.' Produce that single short line, then end your turn immediately. Say "
                                "nothing else: do not say it twice, do not answer any question, state any price, "
                                "number, or decision, introduce a new topic, repeat the pending question, or react "
                                "to other representative speech."
                            ),
                        },
                    },
                    wait_for_available=True,
                    purpose="keepalive",
                )
                self._events.append("realtime.waiting_keepalive", {"task_id": task_id})
                try:
                    await asyncio.wait_for(
                        session.keepalive_complete.wait(),
                        timeout=max(self._waiting_keepalive_interval, 5),
                    )
                except TimeoutError:
                    await session.realtime.send(json.dumps({"type": "response.cancel"}))
                    session.keepalive_response_active = False
                    session.keepalive_complete.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            session.keepalive_response_active = False
            session.keepalive_complete.set()
            self._events.append(
                "realtime.waiting_keepalive_failed",
                {"task_id": task_id, "reason": type(error).__name__},
            )


def gatekeeper_debug_payload(request: GatekeeperRequest) -> dict[str, Any]:
    return {
        "instructions": request.instructions,
        "latest_utterance": request.latest_utterance,
        "context": request.context,
        "context_updates": list(request.context_updates),
        "messages": request.messages(),
    }
