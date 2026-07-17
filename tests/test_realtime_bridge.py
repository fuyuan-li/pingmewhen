import asyncio
import json

from relay_agent.credentials import RelayCredentials
from relay_agent.event_log import EventLog
from relay_agent.realtime_bridge import (
    RealtimeSessionHub,
    initial_response,
    private_instruction,
    realtime_session_update,
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


def test_live_instruction_is_injected_and_requests_a_natural_response(tmp_path):
    class FakeRealtime:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(json.loads(value))

    realtime = FakeRealtime()
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )

    async def run():
        hub._sessions["task-1"] = realtime
        return await hub.inject("task-1", "Ask about a discount.")

    assert asyncio.run(run()) is True
    assert [event["type"] for event in realtime.sent] == ["conversation.item.create", "response.create"]
