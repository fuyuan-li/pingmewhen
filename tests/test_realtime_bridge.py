import asyncio
import json

from relay_agent.credentials import RelayCredentials
from relay_agent.event_log import EventLog
from relay_agent.realtime_bridge import (
    ActiveRealtimeSession,
    RealtimeSessionHub,
    initial_response,
    private_instruction,
    realtime_session_update,
    requested_sensitive_field,
    transcript_from_realtime_event,
)


def sample_context():
    return {
        "goal": "Gather a factual service quote.",
        "action": {
            "target": "Example Provider",
            "purpose": "Ask for price and availability.",
        },
    }


def test_realtime_session_uses_twilio_native_pcmu_and_opens_the_call():
    update = realtime_session_update(sample_context())

    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert update["session"]["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert "disclose" in update["session"]["instructions"].lower()
    assert initial_response()["type"] == "response.create"


def test_private_instruction_is_context_only_and_transcripts_are_classified():
    instruction = private_instruction("Ask about a multi-policy discount.")

    assert instruction["type"] == "conversation.item.create"
    assert "multi-policy" in instruction["item"]["content"][0]["text"]
    assert transcript_from_realtime_event(
        {"type": "response.output_audio_transcript.done", "transcript": "Hello"}
    ) == ("relay", "Hello")
    assert transcript_from_realtime_event(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hi"}
    ) == ("representative", "Hi")
    assert requested_sensitive_field("May I have the card number?") == "card_number"
    assert requested_sensitive_field("What is the CVV?") == "cvv"
    assert requested_sensitive_field("May I repeat that number back to verify it?") == "verification_request"
    assert requested_sensitive_field("What are the last four digits of your SSN?") is None


class FakeRealtime:
    def __init__(self, incoming=None):
        self.sent = []
        self.incoming = list(incoming or [])

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return json.dumps(self.incoming.pop(0))


class FakeTwilio:
    def __init__(self, incoming=None):
        self.sent = []
        self.incoming = list(incoming or [])

    async def send_json(self, value):
        self.sent.append(value)

    async def receive_json(self):
        return self.incoming.pop(0)


def test_live_instruction_is_injected_and_requests_a_natural_response(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1")
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )

    async def run():
        hub._sessions["task-1"] = session
        return await hub.inject("task-1", "Ask about a discount.")

    assert asyncio.run(run()) is True
    assert [event["type"] for event in realtime.sent] == ["conversation.item.create", "response.create"]


def test_secure_mode_stops_audio_in_both_directions_and_suppresses_transcripts(tmp_path):
    representative_audio = FakeTwilio(
        [
            {"event": "media", "media": {"payload": "sensitive-inbound-audio"}},
            {"event": "stop"},
        ]
    )
    realtime = FakeRealtime(
        [
            {"type": "response.output_audio.delta", "delta": "sensitive-outbound-audio"},
            {"type": "response.output_audio_transcript.done", "transcript": "four two four two"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "The card number is 4242 4242 4242 4242",
            },
        ]
    )
    session = ActiveRealtimeSession(realtime, representative_audio, "MZ1", secure_mode=True)
    transcripts = []
    log_path = tmp_path / "events.jsonl"
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: transcripts.append((speaker, text)),
        EventLog(log_path),
    )

    async def run():
        await asyncio.gather(hub._twilio_to_openai(session), hub._openai_to_twilio(session, "task-1"))

    asyncio.run(run())

    assert not any(event.get("type") == "input_audio_buffer.append" for event in realtime.sent)
    assert not any(message.get("event") == "media" for message in representative_audio.sent)
    assert transcripts == []
    assert not log_path.exists() or "4242" not in log_path.read_text()


def test_sensitive_request_disconnects_realtime_before_accepting_a_field(tmp_path):
    requested = []
    transcripts = []
    realtime = FakeRealtime(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Please provide the card number.",
            }
        ]
    )
    twilio = FakeTwilio()
    session = ActiveRealtimeSession(realtime, twilio, "MZ1")
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: transcripts.append(text),
        EventLog(tmp_path / "events.jsonl"),
        secure_requester=lambda task_id, field: requested.append((task_id, field)) or {"call_state": "SECURE_LOCAL"},
    )

    asyncio.run(hub._openai_to_twilio(session, "task-1"))

    assert session.secure_mode is True
    assert requested == [("task-1", "card_number")]
    assert transcripts == []
    assert [message["event"] for message in twilio.sent] == ["clear"]
    assert [event["type"] for event in realtime.sent] == ["response.cancel", "input_audio_buffer.clear"]


def test_secure_local_tts_sends_one_field_to_twilio_then_resumes(tmp_path):
    class FakeRenderer:
        def __init__(self):
            self.calls = []

        def render(self, field, value):
            self.calls.append((field, value))
            return ["cGNtdS1hdWRpbw=="]

    renderer = FakeRenderer()
    realtime = FakeRealtime()
    twilio = FakeTwilio()
    session = ActiveRealtimeSession(
        realtime,
        twilio,
        "MZ1",
        secure_mode=True,
        expected_field="card_number",
    )
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        tts_renderer=renderer,
        playback_timeout=1,
    )

    async def run():
        hub._sessions["task-1"] = session
        speaking = asyncio.create_task(hub.speak_secure_field("task-1", "card_number", "4242424242424242"))
        while not session.mark_events:
            await asyncio.sleep(0)
        next(iter(session.mark_events.values())).set()
        await speaking
        await hub.resume_after_secure_field("task-1")

    asyncio.run(run())

    assert renderer.calls == [("card_number", "4242424242424242")]
    assert [message["event"] for message in twilio.sent] == ["media", "mark"]
    assert session.secure_mode is False
    assert [event["type"] for event in realtime.sent] == ["input_audio_buffer.clear", "response.create"]
