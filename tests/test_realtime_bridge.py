import asyncio
import json

from relay_agent.credentials import RelayCredentials
from relay_agent.event_log import EventLog
from relay_agent.gatekeeper import AuthorityVeto, ContextUpdate, GatekeeperVerdict, PrivateMessageRoute
from relay_agent.realtime_bridge import (
    ActiveRealtimeSession,
    RealtimeSessionHub,
    initial_response,
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
    context["caller_name"] = "mina"
    update = realtime_session_update(context)

    assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert update["session"]["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    instructions = update["session"]["instructions"]
    assert 'You represent (you are calling ON BEHALF OF): "Mina"' in instructions
    assert 'You are calling (the organization/person you dial and speak to): "Example Provider"' in instructions
    assert 'Never say you are calling "Mina"' in instructions
    assert "There is no opening phase to finish" in instructions
    assert "treat the interruption as normal barge-in" in instructions
    assert "outbound caller" in instructions
    assert "spoken audio is always addressed to the representative" in instructions
    assert "Private text comes from the person you represent" in instructions
    assert "Never acknowledge or answer that private person aloud" in instructions
    assert "never restart, complete, or repeat the introduction or disclosure" in instructions
    assert "Never deny being an AI if asked" in instructions
    assert "backend Gatekeeper is the sole authority" in instructions
    assert "Gatekeeper is the sole authority" in instructions
    assert "Never invent a missing fact" in instructions
    assert "Do not provide payment-card data or a full Social Security number" in instructions
    assert "Never read phone numbers, account numbers, or other reference identifiers aloud" in instructions
    assert "internal reference by default" in instructions
    assert "Do not choose a regulated product for the user" in instructions
    assert "Never choose, accept, reject, counter, approve, schedule, enroll, purchase, cancel" in instructions
    assert "A budget, preference, or overall goal is not approval" in instructions
    assert "Do not front-load every detail" in instructions
    assert "one or two short sentences" in instructions
    assert "Always refer to the represented person by the exact name" in instructions
    assert "Never replace \"Mina\" with 'the customer,'" in instructions
    assert "Never vocalize planning, analysis, self-talk" in instructions
    assert "while they follow along by text" not in instructions
    assert update["session"]["tool_choice"] == "none"
    assert update["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
    assert update["session"]["tools"] == []
    opening = initial_response(context)
    assert opening["type"] == "response.create"
    assert "You are calling Example Provider" in opening["response"]["instructions"]
    assert "on behalf of Mina" in opening["response"]["instructions"]
    assert "do not say you are calling Mina" in opening["response"]["instructions"]
    assert "ask permission to continue" in opening["response"]["instructions"]
    assert "Hi, Relay here — I'm an AI assistant on behalf of Mina." in opening["response"]["instructions"]
    assert "following along" not in opening["response"]["instructions"]
    assert "two short sentences" in opening["response"]["instructions"]
    assert "first audible words must be 'Hi, Relay here.'" in opening["response"]["instructions"]
    assert "Do not vocalize planning, analysis, self-talk" in opening["response"]["instructions"]
    assert "never restart or finish this first turn later" in opening["response"]["instructions"]


def test_realtime_session_uses_selected_transcription_model():
    update = realtime_session_update(sample_context(), "gpt-4o-transcribe")

    assert update["session"]["audio"]["input"]["transcription"]["model"] == "gpt-4o-transcribe"


def test_realtime_session_has_no_persistent_opening_obligation():
    instructions = realtime_session_update(sample_context())["session"]["instructions"]

    assert "There is no opening phase to finish" in instructions
    assert "never restart, complete, or repeat the introduction" in instructions
    assert "OPENING (say once" not in instructions


def test_transcripts_are_classified():
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


class SequenceGatekeeper:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.requests = []
        self.veto_requests = []

    async def classify(self, request):
        self.requests.append(request)
        return self.verdicts.pop(0)

    async def veto(self, request):
        self.veto_requests.append(request)
        return AuthorityVeto(requires_user=False, reason="none")

    async def route_private_message(self, request):
        return PrivateMessageRoute(
            disposition="answer" if request.waiting_for_user else "call_instruction",
            speaker_update=ContextUpdate(
                kind="fact" if request.waiting_for_user else "call_instruction",
                key="user_answer" if request.waiting_for_user else "call_instruction",
                value=request.text,
                summary=request.text,
            ),
        )


class BlockingGatekeeper:
    def __init__(self, verdict):
        self.verdict = verdict
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def classify(self, request):
        self.started.set()
        await self.release.wait()
        return self.verdict

    async def veto(self, request):
        return AuthorityVeto(requires_user=False, reason="none")

    async def route_private_message(self, request):
        raise AssertionError("Private routing was not expected")


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


def test_live_instruction_is_reformulated_into_session_context_before_response(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )

    async def run():
        hub._sessions["task-1"] = session
        return await hub.inject("task-1", "Ask about a discount.")

    delivery = asyncio.run(run())
    assert delivery.disposition == "call_instruction"
    assert [event["type"] for event in realtime.sent] == ["conversation.item.create", "response.create"]
    assert not any(event.get("type") == "session.update" for event in realtime.sent)
    context_text = realtime.sent[0]["item"]["content"][0]["text"]
    assert "Ask about a discount." in context_text
    assert "CONFIRMED CONTEXT UPDATE FROM RELAY BACKEND" in context_text
    assert len(realtime.sent[0]["item"]["id"]) == 32
    response_instruction = realtime.sent[-1]["response"]["instructions"]
    assert "CONFIRMED CONTEXT UPDATE conversation item" in response_instruction
    assert "acknowledge the represented person aloud" in response_instruction.lower()


def test_context_update_requires_matching_conversation_item_ack_before_response(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        session_update_timeout=0.01,
    )

    async def run():
        hub._sessions["task-1"] = session
        try:
            await hub.inject("task-1", "Ask about a discount.")
        except RuntimeError as error:
            return str(error)
        raise AssertionError("Missing conversation-item acknowledgment should fail.")

    error = asyncio.run(run())

    assert "did not acknowledge the confirmed context update" in error
    assert [event["type"] for event in realtime.sent] == ["conversation.item.create"]
    assert session.context_updates == []


def test_matching_conversation_item_added_event_acknowledges_dynamic_context(tmp_path):
    realtime = FakeRealtime(
        [{"type": "conversation.item.added", "item": {"id": "relay_context_known"}}]
    )
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1")
    session.pending_context_item_id = "relay_context_known"
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )

    asyncio.run(hub._openai_to_twilio(session, "task-1"))

    assert session.context_item_ack.is_set()


def test_meta_private_question_stays_in_private_workspace_and_never_reaches_speaker(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1")
    class MetaGatekeeper(SequenceGatekeeper):
        async def route_private_message(self, request):
            return PrivateMessageRoute(
                disposition="private_meta",
                private_reply="I am Relay, your private call assistant.",
            )

    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=MetaGatekeeper([]),
    )

    async def run():
        hub._sessions["task-1"] = session
        return await hub.inject("task-1", "Who are you?")

    delivery = asyncio.run(run())
    assert delivery.disposition == "private_meta"
    assert delivery.private_reply == "I am Relay, your private call assistant."
    assert realtime.sent == []


def test_speaker_waits_for_answerable_gatekeeper_verdict_before_responding(tmp_path):
    gatekeeper = BlockingGatekeeper(GatekeeperVerdict(route="continue", reason="none"))
    realtime = FakeRealtime(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "What plans are available?",
            }
        ]
    )
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
    )

    async def run():
        forwarding = asyncio.create_task(hub._openai_to_twilio(session, "task-1"))
        await gatekeeper.started.wait()
        assert not any(event.get("type") == "response.create" for event in realtime.sent)
        gatekeeper.release.set()
        await forwarding

    asyncio.run(run())

    assert [event["type"] for event in realtime.sent] == ["response.create"]


def test_representative_turn_does_not_overlap_opening_response(tmp_path):
    gatekeeper = SequenceGatekeeper([GatekeeperVerdict(route="continue", reason="none")])
    realtime = FakeRealtime([{"type": "response.done"}])
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    session.response_pending = True
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
    )

    async def run():
        await hub._gate_representative_turn(session, "task-1", "Hello")
        assert realtime.sent == []
        assert session.deferred_representative_turns == ["Hello"]
        await hub._openai_to_twilio(session, "task-1")

    asyncio.run(run())

    assert realtime.sent == [{"type": "response.create"}]
    assert gatekeeper.requests[0].latest_utterance == "Hello"


def test_trivial_acknowledgement_bypasses_gatekeeper_without_private_prompt(tmp_path):
    gatekeeper = SequenceGatekeeper([])
    realtime = FakeRealtime()
    log_path = tmp_path / "events.jsonl"
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(log_path),
        gatekeeper=gatekeeper,
    )

    asyncio.run(hub._gate_representative_turn(session, "task-1", "Mhm."))

    assert gatekeeper.requests == []
    assert realtime.sent == [{"type": "response.create"}]
    assert logged_events(log_path)[0]["event"] == "gatekeeper.bypassed"


def test_material_offer_is_shown_faithfully_and_waits_for_user_decision(tmp_path):
    gatekeeper = SequenceGatekeeper(
        [
            GatekeeperVerdict(
                route="consult_user",
                reason="decision",
                representative_update=(
                    "Alex offered $100 per month, conditioned on enrolling with a contract device."
                ),
                question_to_user="Do you want Relay to accept, counter, or decline?",
            )
        ]
    )
    prompts = []
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
        user_input_requester=lambda task_id, question, kind, blocking, *metadata: prompts.append(question) or {},
    )

    asyncio.run(hub._gate_representative_turn(session, "task-1", "I can do $100 with a contract device."))

    assert session.waiting_for_user is True
    assert "$100 per month" in prompts[0]
    assert "accept, counter, or decline" in prompts[0]
    assert realtime.sent[0]["type"] == "response.create"
    assert "brief natural hold line" in realtime.sent[0]["response"]["instructions"]


def test_authority_veto_blocks_a_gatekeeper_continuation_before_speaker_responds(tmp_path):
    class VetoingGatekeeper(SequenceGatekeeper):
        async def veto(self, request):
            self.veto_requests.append(request)
            return AuthorityVeto(
                requires_user=True,
                reason="approval",
                representative_update="The representative is ready to submit enrollment.",
                question_to_user="Do you approve submitting the enrollment?",
            )

    prompts = []
    gatekeeper = VetoingGatekeeper([GatekeeperVerdict(route="continue", reason="none")])
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
        user_input_requester=lambda *args: prompts.append(args) or {},
    )

    asyncio.run(hub._gate_representative_turn(session, "task-1", "Should I submit enrollment now?"))

    assert len(gatekeeper.veto_requests) == 1
    assert session.waiting_for_user is True
    assert session.pending_interaction_reason == "approval"
    assert len(session.pending_interaction_id) == 32
    assert "ready to submit enrollment" in prompts[0][1]
    assert [event["type"] for event in realtime.sent] == ["response.create"]
    assert "brief natural hold line" in realtime.sent[0]["response"]["instructions"]


def test_response_lifecycle_logs_request_purpose_and_server_response_id(tmp_path):
    log_path = tmp_path / "events.jsonl"
    realtime = FakeRealtime(
        [
            {"type": "response.created", "response": {"id": "resp-123"}},
            {"type": "response.done", "response": {"id": "resp-123", "status": "completed"}},
        ]
    )
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", task_id="task-1")
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(log_path),
    )

    async def run():
        await hub._send_response_create(session, purpose="private_answer")
        await hub._openai_to_twilio(session, "task-1")

    asyncio.run(run())

    events = logged_events(log_path)
    assert [event["event"] for event in events] == [
        "realtime.response_requested",
        "realtime.response_created",
        "realtime.response_completed",
    ]
    assert events[0]["payload"]["purpose"] == "private_answer"
    assert events[1]["payload"]["response_id"] == "resp-123"
    assert events[2]["payload"]["status"] == "completed"


def test_gatekeeper_receives_only_approved_call_facts_not_private_planning_history(tmp_path):
    gatekeeper = SequenceGatekeeper([GatekeeperVerdict(route="continue", reason="none")])
    realtime = FakeRealtime()
    context = {
        **sample_context(),
        "goal": "Private planning history with an unrelated phone number +12025550123.",
        "private_messages": ["Unrelated planning conversation"],
        "prior_call_transcript": "Unrelated old call",
        "document_context": "Account tier: standard",
    }
    context["action"]["known_facts"] = ["Service address: 123 Main Street"]
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=context)
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: context,
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
    )

    asyncio.run(hub._gate_representative_turn(session, "task-1", "What plans are available?"))

    supplied = gatekeeper.requests[0].context
    assert supplied["approved_call"]["known_facts"] == ["Service address: 123 Main Street"]
    assert supplied["document_context"] == "Account tier: standard"
    assert "goal" not in supplied
    assert "private_messages" not in supplied
    assert "prior_call_transcript" not in supplied


def test_waiting_for_user_keeps_listening_and_transcribing_without_another_response(tmp_path):
    gatekeeper = SequenceGatekeeper([])
    transcripts = []
    realtime = FakeRealtime(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Take your time. I will stay on the line.",
            }
        ]
    )
    twilio = FakeTwilio(
        [
            {"event": "media", "media": {"payload": "representative-audio-during-wait"}},
            {"event": "stop"},
        ]
    )
    session = ActiveRealtimeSession(
        realtime,
        twilio,
        "MZ1",
        waiting_for_user=True,
        context=sample_context(),
    )
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: transcripts.append((task_id, speaker, text)) or {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
    )

    async def run():
        await asyncio.gather(
            hub._twilio_to_openai(session),
            hub._openai_to_twilio(session, "task-1"),
        )

    asyncio.run(run())

    assert transcripts == [
        ("task-1", "representative", "Take your time. I will stay on the line."),
    ]
    assert gatekeeper.requests == []
    assert realtime.sent == [
        {"type": "input_audio_buffer.append", "audio": "representative-audio-during-wait"},
    ]
    assert not any(event.get("type") == "response.create" for event in realtime.sent)


def test_waiting_for_user_repeats_short_keepalive_after_hold_line(tmp_path):
    gatekeeper = SequenceGatekeeper(
        [
            GatekeeperVerdict(
                route="consult_user",
                reason="missing_fact",
                representative_update="The representative asked for the apartment number.",
                question_to_user="What is your apartment number?",
            )
        ]
    )
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
        user_input_requester=lambda *args: {},
        waiting_keepalive_interval=0.01,
    )

    async def run():
        await hub._gate_representative_turn(session, "task-1", "What is the apartment number?")
        assert len(realtime.sent) == 1
        session.response_pending = False
        session.response_complete.set()
        session.hold_response_active = False
        session.hold_complete.set()
        while len(realtime.sent) < 2:
            await asyncio.sleep(0)
        first_keepalive = realtime.sent[1]
        assert session.waiting_for_user is True
        assert session.keepalive_response_active is True
        session.response_pending = False
        session.response_complete.set()
        session.keepalive_response_active = False
        session.keepalive_complete.set()
        while len(realtime.sent) < 3:
            await asyncio.sleep(0)
        session.waiting_for_user = False
        session.keepalive_response_active = False
        session.keepalive_complete.set()
        await hub._stop_waiting_keepalive(session)
        return first_keepalive

    keepalive = asyncio.run(run())

    assert keepalive["type"] == "response.create"
    instruction = keepalive["response"]["instructions"]
    assert "exactly one short natural keep-alive line" in instruction
    assert "do not answer any question" in instruction


def test_unanswerable_gatekeeper_turn_waits_then_accumulates_user_updates(tmp_path):
    gatekeeper = SequenceGatekeeper(
        [
            GatekeeperVerdict(
                route="consult_user",
                reason="missing_fact",
                representative_update="The representative asked for the apartment number.",
                question_to_user="What is your apartment number?",
            ),
            GatekeeperVerdict(
                route="consult_user",
                reason="preference",
                representative_update="The representative asked for an installation date.",
                question_to_user="What installation date works?",
            ),
        ]
    )
    prompts = []
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(realtime, FakeTwilio(), "MZ1", context=sample_context())
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        gatekeeper=gatekeeper,
        user_input_requester=lambda task_id, question, kind, blocking, *metadata: prompts.append(
            (question, kind, blocking)
        )
        or {},
    )

    async def run():
        await hub._gate_representative_turn(session, "task-1", "What is the apartment number?")
        session.response_pending = False
        session.response_complete.set()
        session.hold_response_active = False
        session.hold_complete.set()
        hub._sessions["task-1"] = session
        assert (await hub.inject("task-1", "Apartment 4B")).resumed_call is True
        session.response_pending = False
        session.response_complete.set()
        await hub._gate_representative_turn(session, "task-1", "What installation date works?")
        session.response_pending = False
        session.response_complete.set()
        session.hold_response_active = False
        session.hold_complete.set()
        assert (await hub.inject("task-1", "Friday afternoon")).resumed_call is True

    asyncio.run(run())

    assert prompts == [
        (
            "The representative asked for the apartment number.\n\nWhat is your apartment number?",
            "text",
            True,
        ),
        (
            "The representative asked for an installation date.\n\nWhat installation date works?",
            "text",
            True,
        ),
    ]
    assert session.waiting_for_user is False
    assert [update["value"] for update in session.context_updates] == ["Apartment 4B", "Friday afternoon"]
    assert gatekeeper.requests[0].context_updates == ()
    assert gatekeeper.requests[1].context_updates[0]["value"] == "Apartment 4B"
    assert [event["type"] for event in realtime.sent] == [
        "response.create",
        "conversation.item.create",
        "response.create",
        "response.create",
        "conversation.item.create",
        "response.create",
    ]


def test_answering_gatekeeper_question_resumes_with_structured_context_only(tmp_path):
    realtime = FakeRealtime()
    session = ActiveRealtimeSession(
        realtime,
        FakeTwilio(),
        "MZ1",
        waiting_for_user=True,
        pending_question="What is your apartment number?",
        context=sample_context(),
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

    delivery = asyncio.run(run())
    assert delivery.resumed_call is True
    assert session.waiting_for_user is False
    assert session.pending_question == ""
    assert session.context_updates[0]["value"] == "Apartment 4B"
    assert [event["type"] for event in realtime.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    assert "Apartment 4B" in realtime.sent[0]["item"]["content"][0]["text"]
    assert "Apartment 4B" not in json.dumps(realtime.sent[1])
    assert not any(event.get("type") == "session.update" for event in realtime.sent)


def test_pending_question_clears_only_after_private_answer_response_completes(tmp_path):
    realtime = FakeRealtime(
        [
            {"type": "response.created", "response": {"id": "resp-answer"}},
            {"type": "response.done", "response": {"id": "resp-answer", "status": "completed"}},
        ]
    )
    session = ActiveRealtimeSession(
        realtime,
        FakeTwilio(),
        "MZ1",
        waiting_for_user=True,
        pending_question="What installation date works?",
        context=sample_context(),
    )
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        response_delivery_timeout=1,
    )

    async def run():
        hub._sessions["task-1"] = session
        delivery_task = asyncio.create_task(hub.inject("task-1", "Aug 1"))
        while not any(event.get("type") == "response.create" for event in realtime.sent):
            await asyncio.sleep(0)
        assert session.waiting_for_user is True
        await hub._openai_to_twilio(session, "task-1")
        return await delivery_task

    delivery = asyncio.run(run())

    assert delivery.resumed_call is True
    assert session.waiting_for_user is False
    assert session.pending_question == ""


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
    assert [event["type"] for event in realtime.sent] == ["input_audio_buffer.clear"]


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
    assert [event["type"] for event in realtime.sent] == ["response.create"]


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
    assert [event["type"] for event in realtime.sent].count("session.update") == 1
    assert realtime.cancelled is True
    assert "task-1" not in hub._sessions
    assert [event["event"] for event in events] == [
        "realtime.response_requested",
        "realtime.connected",
        "realtime.disconnected",
    ]


def test_bridge_debug_trace_captures_exact_speaker_and_gatekeeper_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "relay-data"))
    monkeypatch.setenv("RELAY_DEBUG_CALL_CONTEXT", "1")
    gatekeeper = SequenceGatekeeper([GatekeeperVerdict(route="continue", reason="none")])
    realtime = FakeRealtime(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "What is the installation address?",
            },
            {"type": "response.done"},
        ]
    )
    twilio = BlockingTwilio([start_message()])
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: {
            **sample_context(),
            "caller_name": "David",
            "private_messages": ["Install at 1079 Commonwealth Ave"],
            "action": {
                **sample_context()["action"],
                "known_facts": ["Installation address: 1079 Commonwealth Ave"],
            },
        },
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
        connector=FakeConnector(realtime),
        gatekeeper=gatekeeper,
    )

    asyncio.run(hub.bridge(twilio))

    debug_path = next((tmp_path / "relay-data" / "debug" / "calls").glob("*.jsonl"))
    records = [json.loads(line) for line in debug_path.read_text().splitlines()]
    events = [record["event"] for record in records]
    assert events == [
        "speaker.context",
        "speaker.response_requested",
        "speaker.response_completed",
        "gatekeeper.request",
        "gatekeeper.verdict",
        "gatekeeper.authority_veto",
        "speaker.response_requested",
    ]
    speaker_context = next(record for record in records if record["event"] == "speaker.context")
    gatekeeper_request_record = next(record for record in records if record["event"] == "gatekeeper.request")
    assert speaker_context["payload"]["context"]["caller_name"] == "David"
    assert speaker_context["payload"]["session_update"]["session"]["instructions"]
    assert gatekeeper_request_record["payload"]["latest_utterance"] == "What is the installation address?"
    assert "1079 Commonwealth Ave" in json.dumps(gatekeeper_request_record, ensure_ascii=False)


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


def test_gatekeeper_identity_extracts_caller_and_target_with_fallbacks():
    from relay_agent.realtime_bridge import gatekeeper_identity

    represented, representative = gatekeeper_identity(
        {"caller_name": "ryan", "action": {"target": "Alex / Verizon"}}
    )
    assert represented == "Ryan"
    assert representative == "Alex / Verizon"

    default_represented, default_representative = gatekeeper_identity({})
    assert default_represented == "the represented user"
    assert default_representative == "the representative"


def test_interrupted_opening_reminds_speaker_not_to_repeat_it(tmp_path):
    hub = RealtimeSessionHub(
        lambda: RelayCredentials(openai_api_key="sk-test"),
        lambda task_id, index: sample_context(),
        lambda task_id, speaker, text: {},
        EventLog(tmp_path / "events.jsonl"),
    )
    session = ActiveRealtimeSession(FakeRealtime(), FakeTwilio(), "MZ1")

    assert hub._representative_turn_request(session) is None

    session.opening_interrupted = True
    request = hub._representative_turn_request(session)

    assert request is not None
    assert "interrupted" in request["response"]["instructions"]
    assert "Do not restart or repeat" in request["response"]["instructions"]
    assert session.opening_interrupted is False
