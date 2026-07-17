from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from relay_agent.credentials import RelayCredentials
from relay_agent.event_log import EventLog


def realtime_session_update(context: dict[str, Any]) -> dict[str, Any]:
    action = context["action"]
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
                    "transcription": {"model": "gpt-4o-mini-transcribe", "language": "en"},
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
    ) -> None:
        self._credentials = credentials
        self._context_reader = context_reader
        self._transcript_writer = transcript_writer
        self._events = events
        self._connector = connector
        self._sessions: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def inject(self, task_id: str, text: str) -> bool:
        async with self._lock:
            realtime = self._sessions.get(task_id)
        if realtime is None:
            return False
        await realtime.send(json.dumps(private_instruction(text)))
        await realtime.send(
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
                async with self._lock:
                    self._sessions[task_id] = realtime
                await realtime.send(json.dumps(realtime_session_update(context)))
                await realtime.send(json.dumps(initial_response()))
                self._events.append("realtime.connected", {"task_id": task_id, "model": model})
                to_openai = asyncio.create_task(self._twilio_to_openai(twilio, realtime))
                to_twilio = asyncio.create_task(self._openai_to_twilio(realtime, twilio, task_id, stream_sid))
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
                if self._sessions.get(task_id) is realtime:
                    self._sessions.pop(task_id, None)
            self._events.append("realtime.disconnected", {"task_id": task_id})

    async def _receive_start(self, twilio: WebSocket) -> dict[str, Any]:
        while True:
            message = await twilio.receive_json()
            if message.get("event") == "start":
                return message
            if message.get("event") == "stop":
                raise ValueError("Stream stopped before it started.")

    async def _twilio_to_openai(self, twilio: WebSocket, realtime: Any) -> None:
        while True:
            message = await twilio.receive_json()
            event = message.get("event")
            if event == "media":
                await realtime.send(
                    json.dumps({"type": "input_audio_buffer.append", "audio": message["media"]["payload"]})
                )
            elif event == "stop":
                return

    async def _openai_to_twilio(self, realtime: Any, twilio: WebSocket, task_id: str, stream_sid: str) -> None:
        async for raw in realtime:
            event = json.loads(raw)
            event_type = event.get("type")
            if event_type == "response.output_audio.delta":
                await twilio.send_json(
                    {"event": "media", "streamSid": stream_sid, "media": {"payload": event["delta"]}}
                )
            elif event_type == "input_audio_buffer.speech_started":
                await twilio.send_json({"event": "clear", "streamSid": stream_sid})
            transcript = transcript_from_realtime_event(event)
            if transcript and transcript[1].strip():
                self._transcript_writer(task_id, transcript[0], transcript[1])
            if event_type == "error":
                detail = event.get("error", {})
                self._events.append(
                    "realtime.protocol_error",
                    {"task_id": task_id, "type": detail.get("type"), "code": detail.get("code")},
                )
