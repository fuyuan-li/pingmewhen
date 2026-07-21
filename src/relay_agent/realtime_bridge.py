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
    "cvv": (r"\bcvv\b", r"\bcvc\b", r"\bccv\b", r"\bsecurity\s+code\b", r"\bcard\s+verification\b"),
    "ssn_last_four": (
        r"\blast\s+(?:four|4)\b.{0,30}\b(?:social security|ssn)\b",
        r"\b(?:social security|ssn)\b.{0,30}\blast\s+(?:four|4)\b",
    ),
    "full_ssn": (
        r"\bssn\b",
        r"\bsocial security\b",
        r"\bsocial\b.{0,15}\bnumber\b",
        r"\b\d{3}[ -]\d{2}[ -]\d{4}\b",
    ),
    "date_of_birth": (r"\bdate of birth\b", r"\bbirth\s*date\b", r"\bdob\b"),
}

SENSITIVE_FIELD_LABELS = {
    "card_number": "card number",
    "expiration": "expiration date",
    "cvv": "security code",
    "full_ssn": "Social Security number",
    "ssn_last_four": "last four digits of the Social Security number",
    "date_of_birth": "date of birth",
    "verification_request": "protected information",
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
    for field_name, patterns in SENSITIVE_FIELD_PATTERNS.items():
        if any(re.search(pattern, lowered) for pattern in patterns):
            return field_name
    return None


def is_trivial_acknowledgement(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s'-]", "", text.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return "?" not in text and cleaned in TRIVIAL_ACKNOWLEDGEMENTS


def sanitized_takeover_text(text: str) -> str:
    cleaned = re.sub(r"(?<!\d)(?:\d[ -]?){7,}(?!\d)", "[redacted identifier]", text)
    return re.sub(
        r"\b(password|passcode|account pin|api key|auth token|authentication token)\b\s*(?:is|:|=)?\s*\S+",
        lambda match: f"{match.group(1)} [REDACTED]",
        cleaned,
        flags=re.I,
    )


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
        "WHO YOU ARE ON THIS REPLY: You are an AI phone caller (never state a product, app, or bot name aloud, and "
        "never give yourself a name), speaking OUT LOUD on a live call to "
        f"{target}. You represent {caller_name} and are calling on their behalf. Speak TO {target}, addressing "
        f"them directly as 'you'. Refer to the person you represent as {caller_name} by name; never call "
        f"{caller_name} 'they', 'them', 'the customer', 'the client', or 'the user', and never talk about "
        f"{caller_name} in the third person. You are not {caller_name} and you are not {target}: you speak for "
        f"{caller_name} to {target}. You already introduced yourself earlier in this call — do not reintroduce "
        "yourself or restate that you are an AI unless asked. Convey the information in the correct direction: keep "
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
    typed_takeover: bool = False
    takeover_sensitive: bool = False
    takeover_exchange: list[dict[str, str]] = field(default_factory=list)
    takeover_speech_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    secure_handoff_field: str | None = None
    takeover_speech_count: int = 0
    response_audio_buffers: dict[str, list[str]] = field(default_factory=dict)
    seen_output_transcript_parts: set[tuple[str, str, int, int]] = field(default_factory=set)
    suppressed_response_ids: set[str] = field(default_factory=set)
    last_forwarded_relay_transcript: str = ""
    last_forwarded_relay_response_id: str = ""
    discard_response_audio: bool = False
    listener_sockets: dict[int, "ListenerChannel"] = field(default_factory=dict)


@dataclass
class ListenerChannel:
    websocket: WebSocket
    queue: asyncio.Queue[dict[str, str]]
    sender_task: asyncio.Task | None = None


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
            "call. Do NOT re-introduce or restate the call's purpose from the top. Simply continue, responding "
            "directly and briefly to what the representative just said. Never deny being an AI if asked. If the "
            "representative asks who you are, who is calling, or to identify yourself, answer plainly: say you are "
            f"an AI assistant calling on behalf of {caller_name} — that is allowed even though you already "
            "opened.\n\n"
        )
    else:
        turn_section = (
            "FIRST TURN: You never speak first. This is an outbound call you placed, so wait for the "
            "representative to speak — a greeting, 'hello,' or asking who is calling — before saying anything. If "
            "instead you hear hold music, an automated queue message, or silence, stay silent and keep waiting; "
            "never fill the silence, greet first, or ask if anyone is there. Once the representative actually "
            "speaks, your very first reply must open with exactly this sentence, word for word, and nothing "
            f"before it: 'Hi, I'm an AI assistant calling on behalf of {caller_name}.' Do not give yourself or "
            "the app any name. Then, in the same reply, briefly state ONLY the primary service or topic you are "
            "calling about (see GOAL below) — for example 'setting up internet service at his address.' Do NOT "
            "announce your negotiation strategy, a target or maximum price, discounts you are hoping for, or your "
            "full multi-step plan in the opening; those come out later only as the conversation needs them. Never "
            "leave the representative wondering why you called or invite generic small talk. Never repeat the "
            "disclosure later in the call, and never restart it if interrupted — continue from what the "
            "representative said. Never deny being an AI if asked.\n\n"
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
        f"ADDRESSING: The person who answered this phone IS {target_literal} — you are speaking directly to them. "
        f"Address them as 'you', always. Never talk about {target_literal} in the third person while speaking to "
        f"them: say 'do you have some options?', never 'does {target_literal} have some options?'. This holds even before "
        f"they state their name. When you relay something {caller_name_literal} wants, keep the direction correct "
        f"— for example, '{caller_name} is hoping you can lower the price', not 'they are hoping they can lower "
        "the price'.\n\n"
        "ROLE: You are the outbound AI caller who initiated this call to accomplish a specific approved goal. "
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
        "When the representative states or quotes any price, monthly charge, rate, fee, deposit, or terms, you must "
        "NOT say it 'works', is 'fine', or accept, agree to, or confirm it — even if it seems reasonable and even "
        "if no budget was set. Acknowledge only that you have noted it; state that a price or terms is accepted "
        "solely when the newest confirmed context explicitly records that exact approval. "
        "CORRECTIONS: when the representative states, repeats, or asks you to confirm a value that your known facts "
        "or the CONFIRMED PRIVATE CONTEXT UPDATES already answer, use that known value to confirm or correct them "
        "— if they read a value back incorrectly, give the correct known value; never treat an already-known fact "
        "as if it were missing or unknown. "
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

    async def attach_listener(self, task_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            session = self._sessions.get(task_id)
            if session is None:
                raise RuntimeError("The live call audio stream is not connected yet.")
            await websocket.accept()
            channel = ListenerChannel(websocket=websocket, queue=asyncio.Queue(maxsize=100))
            session.listener_sockets[id(websocket)] = channel
            channel.sender_task = asyncio.create_task(self._listener_sender(session, channel))
        self._events.append("call.listener_attached", {"task_id": task_id})

    async def detach_listener(self, task_id: str, websocket: WebSocket, close: bool = False) -> None:
        channel = None
        async with self._lock:
            session = self._sessions.get(task_id)
            if session is not None:
                channel = session.listener_sockets.pop(id(websocket), None)
        if channel and channel.sender_task and channel.sender_task is not asyncio.current_task():
            channel.sender_task.cancel()
            await asyncio.gather(channel.sender_task, return_exceptions=True)
        if close:
            with suppress(RuntimeError):
                await websocket.close(code=1000)
        if channel:
            self._events.append("call.listener_detached", {"task_id": task_id})

    async def _listener_sender(self, session: ActiveRealtimeSession, channel: ListenerChannel) -> None:
        try:
            while True:
                await channel.websocket.send_json(await channel.queue.get())
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            session.listener_sockets.pop(id(channel.websocket), None)

    def _fan_out_listener_audio(self, session: ActiveRealtimeSession, source: str, payload: str) -> None:
        frame = {"source": source, "payload": payload}
        for channel in tuple(session.listener_sockets.values()):
            if channel.queue.full():
                with suppress(asyncio.QueueEmpty):
                    channel.queue.get_nowait()
            with suppress(asyncio.QueueFull):
                channel.queue.put_nowait(frame)

    async def _close_session_listeners(self, session: ActiveRealtimeSession) -> None:
        channels = tuple(session.listener_sockets.values())
        session.listener_sockets.clear()
        for channel in channels:
            if channel.sender_task:
                channel.sender_task.cancel()
        if channels:
            await asyncio.gather(
                *(channel.sender_task for channel in channels if channel.sender_task),
                return_exceptions=True,
            )
            for channel in channels:
                with suppress(RuntimeError):
                    await channel.websocket.close(code=1000)

    async def _send_response_create(
        self,
        session: ActiveRealtimeSession,
        request: dict[str, Any] | None = None,
        wait_for_available: bool = False,
        purpose: str = "unspecified",
    ) -> bool:
        if session.typed_takeover or session.secure_mode:
            return False
        request = request or {"type": "response.create"}
        while True:
            async with session.response_lock:
                if not session.response_pending:
                    request_id = uuid4().hex
                    session.response_pending = True
                    session.response_request_id = request_id
                    session.response_server_id = ""
                    session.response_purpose = purpose
                    session.discard_response_audio = False
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
        if session is None or session.secure_mode or session.typed_takeover:
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
                self._discard_buffered_response_audio(session)
                await session.realtime.send(json.dumps({"type": "response.cancel"}))
            session.hold_response_active = False
            session.hold_complete.set()
        if session.keepalive_response_active:
            self._discard_buffered_response_audio(session)
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
                            "CONFIRMED CONTEXT UPDATE FROM PINGMEWHEN BACKEND\n"
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
                    self._discard_buffered_response_audio(session)
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

    async def _cancel_active_response(self, session: ActiveRealtimeSession) -> None:
        if not session.response_pending:
            return
        self._discard_buffered_response_audio(session)
        await session.realtime.send(json.dumps({"type": "response.cancel"}))
        try:
            await asyncio.wait_for(session.response_complete.wait(), timeout=2)
        except TimeoutError:
            session.response_pending = False
            session.response_request_id = ""
            session.response_server_id = ""
            session.response_purpose = ""
            session.response_complete.set()
            self._events.append("realtime.response_cancel_timeout", {"task_id": session.task_id})

    async def _play_local_tts(
        self, session: ActiveRealtimeSession, chunks: list[str], mark_prefix: str, task_id: str = ""
    ) -> None:
        if not chunks:
            raise RuntimeError("Local speech returned no audio.")
        mark_name = f"{mark_prefix}-{uuid4().hex}"
        completion = asyncio.Event()
        session.mark_events[mark_name] = completion
        try:
            try:
                for payload in chunks:
                    await session.twilio.send_json(
                        {"event": "media", "streamSid": session.stream_sid, "media": {"payload": payload}}
                    )
                    self._fan_out_listener_audio(session, "relay", payload)
                await session.twilio.send_json(
                    {"event": "mark", "streamSid": session.stream_sid, "mark": {"name": mark_name}}
                )
            except (WebSocketDisconnect, RuntimeError, OSError) as error:
                raise RuntimeError("The call disconnected while PingMeWhen was speaking.") from error
            # Poll instead of a single long wait_for so a call that disconnects while we're
            # waiting for the playback-complete mark is detected within one poll interval,
            # not only after the full timeout elapses. Skipped when the caller has no task_id
            # to check liveness against (e.g. tests exercising playback in isolation).
            deadline = monotonic() + self._playback_timeout
            poll_interval = min(0.5, self._playback_timeout)
            while monotonic() < deadline:
                if completion.is_set():
                    return
                if task_id:
                    async with self._lock:
                        still_connected = self._sessions.get(task_id) is session
                    if not still_connected:
                        raise RuntimeError("The call disconnected while PingMeWhen was speaking.")
                with suppress(TimeoutError):
                    await asyncio.wait_for(completion.wait(), timeout=poll_interval)
            if not completion.is_set():
                raise TimeoutError("Timed out waiting for the call to confirm local speech playback.")
        finally:
            session.mark_events.pop(mark_name, None)
            chunks.clear()

    async def speak_takeover_text(self, task_id: str, text: str) -> None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None or not session.typed_takeover:
            raise RuntimeError("Type-to-speak is not active for this call.")
        cleaned = text.strip()
        if not cleaned:
            raise RuntimeError("Type something for the representative.")
        async with session.takeover_speech_lock:
            if session.takeover_sensitive:
                field_name = session.expected_field
                if not field_name:
                    raise RuntimeError("PingMeWhen is not waiting for a protected field.")
                if field_name == "verification_request":
                    renderer = getattr(self._tts_renderer, "render_text", None)
                    if renderer is None:
                        raise RuntimeError("The configured local voice cannot speak arbitrary text.")
                    chunks = renderer(cleaned)
                else:
                    renderer = getattr(self._tts_renderer, "render_sensitive", None)
                    chunks = renderer(field_name, cleaned) if renderer else self._tts_renderer.render(field_name, cleaned)
                cleaned = ""
                await self._play_local_tts(session, chunks, "secure-takeover", task_id)
            else:
                renderer = getattr(self._tts_renderer, "render_text", None)
                if renderer is None:
                    raise RuntimeError("The configured local voice cannot speak arbitrary text.")
                chunks = renderer(cleaned)
                await self._play_local_tts(session, chunks, "typed-takeover", task_id)
                session.takeover_exchange.append({"speaker": "user", "text": cleaned})
                cleaned = ""
            session.takeover_speech_count += 1
        self._events.append(
            "call.takeover_speech_completed",
            {"task_id": task_id, "sensitive": session.takeover_sensitive},
        )

    async def enter_typed_takeover(self, task_id: str, sensitive: bool = False) -> None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None:
            raise RuntimeError("The active call is no longer connected.")
        await self._stop_waiting_keepalive(session)
        session.waiting_for_user = False
        session.pending_question = ""
        session.pending_interaction_id = ""
        session.pending_interaction_reason = ""
        session.typed_takeover = True
        session.takeover_sensitive = bool(sensitive or session.secure_mode)
        session.takeover_exchange.clear()
        session.takeover_speech_count = 0
        if session.response_pending:
            with suppress(Exception):
                await self._cancel_active_response(session)
        await session.twilio.send_json({"event": "clear", "streamSid": session.stream_sid})
        if session.takeover_sensitive:
            session.secure_mode = True
            await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        self._events.append(
            "call.takeover_audio_gated",
            {"task_id": task_id, "sensitive": session.takeover_sensitive},
        )

    async def exit_typed_takeover(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None or not session.typed_takeover:
            raise RuntimeError("Typed takeover is not active for this call.")
        sensitive = session.takeover_sensitive
        speech_count = session.takeover_speech_count
        protected_field = session.expected_field or "protected information"
        protected_label = SENSITIVE_FIELD_LABELS.get(protected_field, "protected information")
        caller_name = normalize_display_name(str(session.context.get("caller_name", ""))) or "the caller"
        update_record = None
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        if sensitive:
            if speech_count:
                item_text = (
                    "CONFIRMED CONTEXT UPDATE FROM PINGMEWHEN BACKEND\n"
                    f"{caller_name} supplied the requested {protected_label} through local voice. Treat that field "
                    "as already provided. Never identify, infer, request, or repeat its value."
                )
                resume_guidance = (
                    f"Say one brief transition conveying: 'All right, {caller_name} has provided the "
                    f"{protected_label}. Let's continue.' Do not mention the private interface and do not identify, "
                    "infer, ask for, or repeat the value."
                )
            else:
                item_text = (
                    "CONFIRMED CONTEXT UPDATE FROM PINGMEWHEN BACKEND\n"
                    f"No local speech was played during the protected {protected_label} takeover. Do not imply "
                    "that the field was provided, and do not claim that the represented person refused or was "
                    "uncomfortable."
                )
                resume_guidance = (
                    f"Say one brief factual transition conveying: '{caller_name} has not provided that protected "
                    "detail yet. How would you like to proceed?' Do not claim refusal or discomfort, do not mention "
                    "the private interface, and do not ask for or repeat the protected value."
                )
        else:
            lines = []
            for turn in session.takeover_exchange[-16:]:
                speaker = "represented person" if turn["speaker"] == "user" else "representative"
                cleaned = sanitized_takeover_text(turn["text"])
                lines.append(f"{speaker}: {cleaned[:500]}")
            summary = "\n".join(lines) or "No substantive exchange was captured during typed takeover."
            update_record = {
                "id": uuid4().hex,
                "kind": "takeover_context",
                "key": "typed_takeover_exchange",
                "value": summary,
                "summary": f"During typed takeover:\n{summary}",
            }
            session.context_updates.append(update_record)
            item_text = (
                "CONFIRMED CONTEXT UPDATE FROM PINGMEWHEN BACKEND\n"
                f"Meaning: {update_record['summary']}\n"
                "This is a backend-confirmed continuity note, not speech currently coming from the representative."
            )
            resume_guidance = (
                "Control has just returned to you after a typed takeover. Continue the current phone conversation "
                "naturally from the newest confirmed backend context. Do not reintroduce yourself, repeat the "
                "disclosure, or mention the private interface."
            )
        item_id = uuid4().hex
        session.pending_context_item_id = item_id
        session.context_item_ack.clear()
        await session.realtime.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": item_text}],
                    },
                }
            )
        )
        try:
            if self._session_update_timeout > 0:
                await asyncio.wait_for(session.context_item_ack.wait(), timeout=self._session_update_timeout)
        finally:
            session.pending_context_item_id = ""
        session.expected_field = None
        session.secure_mode = False
        session.typed_takeover = False
        session.takeover_sensitive = False
        session.takeover_exchange.clear()
        session.takeover_speech_count = 0
        await self._send_response_create(
            session,
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        speaker_addressing_preamble(session.context)
                        + resume_guidance
                    )
                },
            },
            wait_for_available=True,
            purpose="takeover_resume",
        )
        self._events.append(
            "call.takeover_context_applied",
            {"task_id": task_id, "sensitive": sensitive, "local_speech_count": speech_count},
        )
        return update_record

    async def _enter_secure_mode(self, task_id: str, session: ActiveRealtimeSession, field_name: str) -> None:
        await self._stop_waiting_keepalive(session)
        session.secure_handoff_field = field_name
        session.expected_field = field_name
        await session.twilio.send_json({"event": "clear", "streamSid": session.stream_sid})
        if session.response_pending:
            self._discard_buffered_response_audio(session)
            await session.realtime.send(json.dumps({"type": "response.cancel"}))
            return
        caller_name = normalize_display_name(str(session.context.get("caller_name", ""))) or "the caller"
        sent = await self._send_response_create(
            session,
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        f"Say exactly this one sentence and nothing else: 'One moment — {caller_name} is going to "
                        "take over for this part.' Do not say, identify, infer, or repeat any protected value."
                    )
                },
            },
            wait_for_available=True,
            purpose="sensitive_handoff",
        )
        if not sent:
            await self._activate_sensitive_takeover(task_id, session, field_name)

    async def _activate_sensitive_takeover(
        self,
        task_id: str,
        session: ActiveRealtimeSession,
        field_name: str,
    ) -> None:
        session.secure_handoff_field = None
        session.secure_mode = True
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        state = self._secure_requester(task_id, field_name) if self._secure_requester else {}
        self._events.append(
            "secure_mode.takeover_required",
            {"task_id": task_id, "field": field_name, "call_state": state.get("call_state", "")},
        )

    async def _promote_active_takeover_to_sensitive(
        self,
        task_id: str,
        session: ActiveRealtimeSession,
        field_name: str,
    ) -> None:
        session.takeover_sensitive = True
        session.secure_mode = True
        session.expected_field = field_name
        session.takeover_exchange.clear()
        session.takeover_speech_count = 0
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        state = self._secure_requester(task_id, field_name) if self._secure_requester else {}
        self._events.append(
            "secure_mode.takeover_required",
            {"task_id": task_id, "field": field_name, "call_state": state.get("call_state", "")},
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
                    await self._close_session_listeners(active_session)
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
                if not session.secure_mode and not session.secure_handoff_field:
                    self._fan_out_listener_audio(session, "representative", message["media"]["payload"])
                    await session.realtime.send(
                        json.dumps({"type": "input_audio_buffer.append", "audio": message["media"]["payload"]})
                    )
            elif event == "mark":
                mark = session.mark_events.get(message.get("mark", {}).get("name", ""))
                if mark:
                    mark.set()
            elif event == "stop":
                return

    def _response_id(self, session: ActiveRealtimeSession, event: dict[str, Any]) -> str:
        response = event.get("response", {})
        return str(
            event.get("response_id")
            or response.get("id", "")
            or session.response_server_id
            or session.response_request_id
            or "active-response"
        )

    def _discard_buffered_response_audio(self, session: ActiveRealtimeSession) -> None:
        session.discard_response_audio = True
        session.response_audio_buffers.clear()

    async def _forward_buffered_response_audio(
        self,
        session: ActiveRealtimeSession,
        response_id: str,
    ) -> None:
        chunks = session.response_audio_buffers.pop(response_id, [])
        if (
            session.secure_mode
            or session.typed_takeover
            or session.discard_response_audio
            or response_id in session.suppressed_response_ids
        ):
            chunks.clear()
            return
        for payload in chunks:
            await session.twilio.send_json(
                {"event": "media", "streamSid": session.stream_sid, "media": {"payload": payload}}
            )
            self._fan_out_listener_audio(session, "relay", payload)
        chunks.clear()

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
                if not session.secure_mode and not session.typed_takeover and (
                    not session.waiting_for_user
                    or session.hold_response_active
                    or session.keepalive_response_active
                ):
                    response_id = self._response_id(session, event)
                    session.response_audio_buffers.setdefault(response_id, []).append(event["delta"])
            elif (
                event_type == "input_audio_buffer.speech_started"
                and not session.secure_mode
                and not session.typed_takeover
                and not session.waiting_for_user
            ):
                self._discard_buffered_response_audio(session)
                await session.twilio.send_json({"event": "clear", "streamSid": session.stream_sid})
            if event_type == "response.done":
                response = event.get("response", {})
                completed_purpose = session.response_purpose
                completed_response_id = str(response.get("id", "")) or session.response_server_id
                if str(response.get("status", "")) == "completed":
                    await self._forward_buffered_response_audio(session, completed_response_id)
                else:
                    session.response_audio_buffers.pop(completed_response_id, None)
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
                session.discard_response_audio = False
                session.response_complete.set()
                if session.hold_response_active:
                    session.hold_response_active = False
                    session.hold_complete.set()
                if session.keepalive_response_active:
                    session.keepalive_response_active = False
                    session.keepalive_complete.set()
                if completed_purpose == "sensitive_handoff" and session.secure_handoff_field:
                    await self._activate_sensitive_takeover(
                        task_id,
                        session,
                        session.secure_handoff_field,
                    )
                elif session.secure_handoff_field:
                    await self._enter_secure_mode(task_id, session, session.secure_handoff_field)
                if (
                    session.deferred_representative_turns
                    and not session.waiting_for_user
                    and not session.typed_takeover
                    and not session.secure_mode
                ):
                    utterance = session.deferred_representative_turns.pop(0)
                    await self._gate_representative_turn(session, task_id, utterance)
            transcript = transcript_from_realtime_event(event)
            if transcript and transcript[1].strip() and not session.secure_mode:
                if transcript[0] == "relay":
                    response_id = self._response_id(session, event)
                    transcript_part = (
                        response_id,
                        str(event.get("item_id", "")),
                        int(event.get("output_index", 0)),
                        int(event.get("content_index", 0)),
                    )
                    if transcript_part in session.seen_output_transcript_parts:
                        self._events.append(
                            "realtime.duplicate_transcript_event_suppressed",
                            {"task_id": task_id, "response_id": response_id},
                        )
                        continue
                    session.seen_output_transcript_parts.add(transcript_part)
                    normalized = " ".join(transcript[1].split())
                    if normalized == session.last_forwarded_relay_transcript:
                        session.suppressed_response_ids.add(response_id)
                        session.response_audio_buffers.pop(response_id, None)
                        self._events.append(
                            "realtime.duplicate_response_suppressed",
                            {
                                "task_id": task_id,
                                "response_id": response_id,
                                "previous_response_id": session.last_forwarded_relay_response_id,
                            },
                        )
                        continue
                    await self._forward_buffered_response_audio(session, response_id)
                    if not session.typed_takeover:
                        self._transcript_writer(task_id, "relay", transcript[1])
                    session.last_forwarded_relay_transcript = normalized
                    session.last_forwarded_relay_response_id = response_id
                    continue
                if session.secure_handoff_field and transcript[0] == "representative":
                    continue
                sensitive_field = requested_sensitive_field(transcript[1])
                if transcript[0] == "representative" and sensitive_field and session.typed_takeover:
                    await self._promote_active_takeover_to_sensitive(task_id, session, sensitive_field)
                elif (
                    transcript[0] == "representative"
                    and sensitive_field
                    and not session.typed_takeover
                    and not session.secure_handoff_field
                ):
                    await self._enter_secure_mode(task_id, session, sensitive_field)
                elif not (session.typed_takeover and transcript[0] == "relay"):
                    self._transcript_writer(task_id, transcript[0], transcript[1])
                    if transcript[0] == "representative" and session.typed_takeover:
                        session.takeover_exchange.append({"speaker": "representative", "text": transcript[1]})
                    elif (
                        transcript[0] == "representative"
                        and not session.waiting_for_user
                        and not session.secure_handoff_field
                    ):
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
        if session.typed_takeover or session.secure_mode or session.secure_handoff_field:
            return
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
                question_to_user="How should PingMeWhen respond?",
            )
        else:
            latency_ms = round((monotonic() - started) * 1000)
        if session.typed_takeover or session.secure_mode or session.secure_handoff_field:
            return
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
                    question_to_user="How should PingMeWhen respond?",
                )
            else:
                veto_latency_ms = round((monotonic() - veto_started) * 1000)
            if session.typed_takeover or session.secure_mode or session.secure_handoff_field:
                return
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
                    self._discard_buffered_response_audio(session)
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
