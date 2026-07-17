from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from relay_agent.credentials import RelayCredentials
from relay_agent.event_log import EventLog
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
    expected_field: str | None = None
    mark_events: dict[str, asyncio.Event] = field(default_factory=dict)


def realtime_session_update(context: dict[str, Any]) -> dict[str, Any]:
    action = context["action"]
    transcription_model = os.environ.get("RELAY_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip()
    private_context = "\n".join(context.get("private_messages", [])[-20:])
    document_context = context.get("document_context", "")
    prior_calls = context.get("prior_call_transcript", "")
    instructions = (
        "You are Relay, speaking on a phone call for the user. Start the call yourself. Clearly disclose that you "
        "are an AI tool speaking for a user who is present by text, state the purpose of the call, and ask whether "
        "the representative is comfortable continuing. Never claim to be the user. Pursue only this approved "
        f"purpose: {action['purpose']}. The overall user goal is: {context['goal']}. The organization being called "
        f"is: {action['target']}. If you need an unknown fact or consequential decision, say 'Can you give me just "
        "one moment?' and wait for a private user instruction. Do not provide payment-card data or a full Social "
        "Security number. Do not choose a regulated product for the user. Be concise and natural. The user's "
        "private task messages are:\n"
        f"{private_context}\nRelevant local document text is:\n{document_context}\nPrior separate call transcript for task "
        f"memory only:\n{prior_calls}\nTreat this as a new representative who has not heard any prior call."
    )
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": transcription_model, "language": "en"},
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": "marin"},
            },
        },
    }


def initial_response() -> dict[str, Any]:
    return {
        "type": "response.create",
        "response": {
            "instructions": "Open the newly connected phone call now with the required disclosure and purpose.",
        },
    }


def private_instruction(text: str) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"Private live instruction from the user: {text}"}],
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
        secure_requester: Callable[[str, str], dict[str, Any]] | None = None,
        call_connected: Callable[[str], dict[str, Any]] | None = None,
        tts_renderer: LocalTTSRenderer | None = None,
        playback_timeout: float = 20,
    ) -> None:
        self._credentials = credentials
        self._context_reader = context_reader
        self._transcript_writer = transcript_writer
        self._events = events
        self._connector = connector
        self._secure_requester = secure_requester
        self._call_connected = call_connected
        self._tts_renderer = tts_renderer or MacOSLocalTTS()
        self._playback_timeout = playback_timeout
        self._sessions: dict[str, ActiveRealtimeSession] = {}
        self._lock = asyncio.Lock()

    async def inject(self, task_id: str, text: str) -> bool:
        async with self._lock:
            session = self._sessions.get(task_id)
        if session is None or session.secure_mode:
            return False
        await session.realtime.send(json.dumps(private_instruction(text)))
        await session.realtime.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "Integrate the new private user instruction naturally into the phone conversation now. "
                            "Do not mention the private channel."
                        )
                    },
                }
            )
        )
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
        await session.realtime.send(json.dumps({"type": "response.cancel"}))
        await session.realtime.send(json.dumps({"type": "input_audio_buffer.clear"}))
        state = self._secure_requester(task_id, field_name) if self._secure_requester else {}
        self._events.append(
            "secure_mode.takeover_required" if state.get("call_state") == "HUMAN_TAKEOVER" else "secure_mode.entered",
            {"task_id": task_id, "field": field_name},
        )

    async def bridge(self, twilio: WebSocket) -> None:
        realtime = None
        await twilio.accept()
        try:
            start = await self._receive_start(twilio)
        except (WebSocketDisconnect, ValueError):
            with suppress(RuntimeError):
                await twilio.close(code=1008)
            return
        stream_sid = start["streamSid"]
        parameters = start["start"].get("customParameters", {})
        task_id = str(parameters.get("task_id", ""))
        try:
            queue_index = int(parameters.get("queue_index", ""))
            context = self._context_reader(task_id, queue_index)
        except Exception:
            await twilio.close(code=1008)
            return
        credentials = self._credentials()
        model = os.environ.get("RELAY_REALTIME_MODEL", "gpt-realtime-2.1")
        url = f"wss://api.openai.com/v1/realtime?model={model}"
        try:
            async with self._connector(
                url,
                additional_headers={"Authorization": f"Bearer {credentials.openai_api_key}"},
            ) as realtime:
                session = ActiveRealtimeSession(realtime=realtime, twilio=twilio, stream_sid=stream_sid)
                async with self._lock:
                    self._sessions[task_id] = session
                if self._call_connected:
                    self._call_connected(task_id)
                await realtime.send(json.dumps(realtime_session_update(context)))
                await realtime.send(json.dumps(initial_response()))
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
            if event_type == "response.output_audio.delta":
                if not session.secure_mode:
                    await session.twilio.send_json(
                        {
                            "event": "media",
                            "streamSid": session.stream_sid,
                            "media": {"payload": event["delta"]},
                        }
                    )
            elif event_type == "input_audio_buffer.speech_started" and not session.secure_mode:
                await session.twilio.send_json({"event": "clear", "streamSid": session.stream_sid})
            transcript = transcript_from_realtime_event(event)
            if transcript and transcript[1].strip() and not session.secure_mode:
                sensitive_field = requested_sensitive_field(transcript[1])
                if transcript[0] == "representative" and sensitive_field:
                    await self._enter_secure_mode(task_id, session, sensitive_field)
                else:
                    self._transcript_writer(task_id, transcript[0], transcript[1])
            if event_type == "error":
                detail = event.get("error", {})
                self._events.append(
                    "realtime.protocol_error",
                    {"task_id": task_id, "type": detail.get("type"), "code": detail.get("code")},
                )
