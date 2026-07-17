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
    context = sample_context()
    context["caller_name"] = "Mina"
    update = realtime_session_update(context)

    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert update["session"]["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    instructions = update["session"]["instructions"]
    assert 'You represent (you are calling ON BEHALF OF): "Mina"' in instructions
    assert 'You are calling (the organization/person you dial and speak to): "Example Provider"' in instructions
    assert 'Never say you are calling "Mina"' in instructions
    assert "say once, at the very start of the call, then never again" in instructions
    assert "outbound caller" in instructions
    assert "spoken audio is always addressed to the representative" in instructions
    assert "Private text comes from the person you represent" in instructions
    assert "Never acknowledge or answer that private person aloud" in instructions
    assert "Do not ask whether the representative is comfortable continuing" in instructions
    assert "never repeat the disclosure" in instructions
    assert "Never deny being an AI if asked" in instructions
    assert "then call request_user_input" in instructions
    assert "The tool call, not a spoken phrase" in instructions
    assert "ask the representative to supply the user's missing fact" in instructions
    assert "Do not provide payment-card data or a full Social Security number" in instructions
    assert "Do not choose a regulated product for the user" in instructions
    assert update["session"]["tool_choice"] == "auto"
    tool = update["session"]["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "request_user_input"
    assert tool["parameters"] == {
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
    }
    opening = initial_response(context)
    assert opening["type"] == "response.create"
    assert "You are calling Example Provider" in opening["response"]["instructions"]
    assert "on behalf of Mina" in opening["response"]["instructions"]
    assert "do not say you are calling Mina" in opening["response"]["instructions"]
    assert "ask permission to continue" in opening["response"]["instructions"]


def test_realtime_session_uses_selected_transcription_model():
    update = realtime_session_update(sample_context(), "gpt-4o-transcribe")

    assert update["session"]["audio"]["input"]["transcription"]["model"] == "gpt-4o-transcribe"


def test_private_instruction_is_context_only_and_transcripts_are_classified():
    instruction = private_instruction("Ask about a multi-policy discount.")

    assert instruction["type"] == "conversation.item.create"
    assert "multi-policy" in instruction["item"]["content"][0]["text"]
    assert "person you represent" in instruction["item"]["content"][0]["text"]
    assert "not from the representative" in instruction["item"]["content"][0]["text"]
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
        self.accepted = False
        self.close_codes = []

    async def accept(self):
        self.accepted = True

    async def close(self, code):
        self.close_codes.append(code)

    async def send_json(self, value):
        self.sent.append(value)

    async def receive_json(self):
        return self.incoming.pop(0)


class FakeConnector:
    def __init__(self, realtime):
        self.realtime = realtime
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self

    async def __aenter__(self):
        return self.realtime

    async def __aexit__(self, exception_type, exception, traceback):
        return False


class BlockingRealtime(FakeRealtime):
    def __init__(self):
        super().__init__()
        self.cancelled = False
        self._blocked = asyncio.Event()

    async def __anext__(self):
        try:
            await self._blocked.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise StopAsyncIteration


class FailingRealtime(FakeRealtime):
    async def __anext__(self):
        raise RuntimeError("Realtime transport failed")


class BlockingTwilio(FakeTwilio):
    def __init__(self, incoming=None):
        super().__init__(incoming)
        self.cancelled = False
        self._blocked = asyncio.Event()

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        try:
            await self._blocked.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise RuntimeError("Twilio receive unexpectedly resumed")


def start_message(task_id="task-1", call_sid="CA1"):
    return {
        "event": "start",
        "streamSid": "MZ1",
        "start": {
            "callSid": call_sid,
            "customParameters": {"task_id": task_id, "queue_index": "0"},
        },
    }


def logged_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


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
    response_instruction = realtime.sent[-1]["response"]["instructions"]
    assert "Continue speaking to the representative you called" in response_instruction
    assert "Do not acknowledge or answer the private person aloud" in response_instruction


def test_request_user_input_tool_pauses_audio_and_notifies_task_state(tmp_path):
    requests = []
    realtime = FakeRealtime(
        [
            {
                "type": "response.function_call_arguments.done",
                "name": "request_user_input",
                "call_id": "call-input-1",
                "arguments": json.dumps(
                    {
                        "question": "What is your apartment number?",
                        "input_kind": "text",
                        "blocking": True,
                    }
                ),
            },
            {"type": "response.output_audio.delta", "delta": "must-not-play"},
        ]
    )
    twilio = FakeTwilio(
        [
            {"event": "media", "media": {"payload": "must-not-forward"}},
            {"event": "stop"},
        ]
    )
    session = ActiveRealtimeSession(realtime, twilio, "MZ1")
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        user_input_requester=lambda task_id, question, input_kind, blocking: requests.append(
            (task_id, question, input_kind, blocking)
        )
        or {},
    )

    async def run():
        await hub._openai_to_twilio(session, "task-1")
        await hub._twilio_to_openai(session)

    asyncio.run(run())

    assert requests == [("task-1", "What is your apartment number?", "text", True)]
    assert session.waiting_for_user is True
    assert session.pending_tool_call_id == "call-input-1"
    assert [event["type"] for event in realtime.sent] == ["input_audio_buffer.clear"]
    assert twilio.sent == []


def test_answering_request_user_input_resumes_and_delivers_answer(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(
        realtime,
        FakeTwilio(),
        "MZ1",
        waiting_for_user=True,
        pending_tool_call_id="call-input-1",
    )
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )

    async def run():
        hub._sessions["task-1"] = session
        return await hub.inject("task-1", "Apartment 4B")

    assert asyncio.run(run()) is True
    assert session.waiting_for_user is False
    assert session.pending_tool_call_id is None
    assert [event["type"] for event in realtime.sent] == [
        "conversation.item.create",
        "conversation.item.create",
        "response.create",
    ]
    assert realtime.sent[0]["item"] == {
        "type": "function_call_output",
        "call_id": "call-input-1",
        "output": json.dumps({"status": "answered"}),
    }
    assert "Apartment 4B" in realtime.sent[1]["item"]["content"][0]["text"]
    response_instruction = realtime.sent[2]["response"]["instructions"]
    assert "person you represent answered privately" in response_instruction
    assert "still speaking aloud to the representative" in response_instruction
    assert "Do not acknowledge the private answer" in response_instruction
    assert "'Got it,'" in response_instruction
    assert "do not address its author as" in response_instruction
    assert "use request_user_input again" in response_instruction


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


def test_last_four_ssn_question_remains_in_normal_transcript_flow(tmp_path):
    requested = []
    transcripts = []
    realtime = FakeRealtime(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "What are the last four digits of your SSN?",
            }
        ]
    )
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1")
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: transcripts.append((task_id, speaker, text)) or {},
        EventLog(tmp_path / "events.jsonl"),
        secure_requester=lambda task_id, field: requested.append((task_id, field)) or {},
    )

    asyncio.run(hub._openai_to_twilio(session, "task-1"))

    assert session.secure_mode is False
    assert session.expected_field is None
    assert requested == []
    assert transcripts == [("task-1", "representative", "What are the last four digits of your SSN?")]


def test_resume_from_takeover_reuses_session_and_injects_fresh_instruction(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(
        realtime,
        FakeTwilio(),
        "MZ1",
        secure_mode=True,
        expected_field="verification_request",
    )
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )

    async def run():
        hub._sessions["task-1"] = session
        await hub.resume_from_takeover("task-1")

    asyncio.run(run())

    assert hub._sessions["task-1"] is session
    assert session.secure_mode is False
    assert session.expected_field is None
    assert [event["type"] for event in realtime.sent] == ["input_audio_buffer.clear", "response.create"]
    assert "handed the active call back" in realtime.sent[-1]["response"]["instructions"]


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


def test_bridge_twilio_stop_cancels_realtime_and_cleans_up_session(tmp_path):
    realtime = BlockingRealtime()
    connector = FakeConnector(realtime)
    twilio = FakeTwilio([start_message(), {"event": "stop"}])
    connected = []
    log_path = tmp_path / "events.jsonl"
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(log_path),
        connector=connector,
        call_connected=lambda task_id: connected.append(task_id) or {},
        realtime_model=lambda: "gpt-realtime-2.1",
        transcription_model=lambda: "gpt-4o-transcribe",
    )

    asyncio.run(hub.bridge(twilio))

    events = logged_events(log_path)
    assert twilio.accepted is True
    assert twilio.close_codes == []
    assert connected == ["task-1"]
    assert connector.calls[0][0].endswith("model=gpt-realtime-2.1")
    assert realtime.sent[0]["session"]["audio"]["input"]["transcription"]["model"] == "gpt-4o-transcribe"
    assert realtime.cancelled is True
    assert "task-1" not in hub._sessions
    assert [event["event"] for event in events] == ["realtime.connected", "realtime.disconnected"]


def test_bridge_rejects_media_start_that_does_not_match_approved_call(tmp_path):
    realtime = FakeRealtime()
    connector = FakeConnector(realtime)
    twilio = FakeTwilio([start_message(call_sid="CAwrong")])
    log_path = tmp_path / "events.jsonl"
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(log_path),
        connector=connector,
    )

    asyncio.run(
        hub.bridge(
            twilio,
            expected_task_id="task-1",
            expected_queue_index=0,
            expected_call_sid="CAexpected",
        )
    )

    assert twilio.accepted is True
    assert twilio.close_codes == [1008]
    assert connector.calls == []
    rejection = logged_events(log_path)[0]
    assert rejection["event"] == "media.identity_rejected"
    assert rejection["payload"] == {
        "check": "call_sid",
        "reason": "ValueError",
        "expected_task_id": "task-1",
        "received_task_id": "task-1",
        "expected_queue_index": 0,
        "received_queue_index": "0",
        "expected_call_sid": "CAexpected",
        "received_call_sid": "CAwrong",
    }


def test_bridge_logs_media_start_failure_before_realtime_connection(tmp_path):
    connector = FakeConnector(FakeRealtime())
    twilio = FakeTwilio([{"event": "stop"}])
    log_path = tmp_path / "events.jsonl"
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(log_path),
        connector=connector,
    )

    asyncio.run(
        hub.bridge(
            twilio,
            expected_task_id="task-1",
            expected_queue_index=0,
            expected_call_sid="CAexpected",
        )
    )

    failure = logged_events(log_path)[0]
    assert failure["event"] == "media.start_failed"
    assert failure["payload"]["reason"] == "ValueError"
    assert failure["payload"]["expected_call_sid"] == "CAexpected"
    assert twilio.close_codes == [1008]
    assert connector.calls == []


def test_bridge_realtime_error_is_reported_and_never_leaves_a_stale_session(tmp_path):
    realtime = FailingRealtime()
    connector = FakeConnector(realtime)
    twilio = BlockingTwilio([start_message()])
    log_path = tmp_path / "events.jsonl"
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(log_path),
        connector=connector,
    )

    asyncio.run(hub.bridge(twilio))

    events = logged_events(log_path)
    failed = next(event for event in events if event["event"] == "realtime.failed")
    assert twilio.accepted is True
    assert twilio.cancelled is True
    assert twilio.close_codes == [1011]
    assert failed["payload"]["reason"] == "RuntimeError"
    assert "task-1" not in hub._sessions
    assert events[-1]["event"] == "realtime.disconnected"
