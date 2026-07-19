import pytest
from fastapi.testclient import TestClient

from relay_agent.agentic_engine import AgenticTaskEngine
from relay_agent.app import create_app
from relay_agent.event_log import EventLog
from relay_agent.planner import PlanAction, PlannerError, PlanningTurn, UnavailablePlanner
from relay_agent.task_engine import InvalidAction
from relay_agent.task_store import SQLiteTaskStore


class FakePlanner:
    ready = True
    model = "fake-planner"

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, goal, messages, contexts):
        self.calls += 1
        if self.calls == 1 and not contexts and not any("123 Main" in message["content"] for message in messages):
            return PlanningTurn(
                status="needs_input",
                message="I need the service address before I can make a call plan.",
                plan_summary="",
                questions=["What service address should PingMeWhen use?"],
            )
        return PlanningTurn(
            status="plan_ready",
            message="I have enough information to propose a plan.",
            plan_summary="Verify three providers, call each for a standardized quote, then return the facts for your decision.",
            caller_name="Taylor",
            actions=[
                PlanAction(
                    kind="research",
                    label="Verify provider contacts",
                    purpose="Find current official customer-service numbers.",
                    target="Three user-approved providers",
                    needs_lookup=True,
                    phone_number="",
                    contact_provided_by="research",
                    contact_source_url="",
                ),
                PlanAction(
                    kind="phone_call",
                    label="Collect standardized quotes",
                    purpose="Ask the same factual coverage and price questions.",
                    target="Example Provider",
                    needs_lookup=False,
                    phone_number="+12025550199",
                    contact_provided_by="research",
                    contact_source_url="https://example.com/contact",
                ),
            ],
        )


def test_agentic_planner_clarifies_then_requires_approval(tmp_path):
    store = SQLiteTaskStore(tmp_path / "relay.db")
    planner = FakePlanner()
    engine = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")

    task = engine.create("Find three renters insurance quotes.")
    assert task["stage"] == "collecting_context"
    assert "address" in task["prompt"]["question"].lower()

    task = engine.act(task["id"], "instruction", "Use 123 Main Street, Washington, DC 20001.")
    assert task["stage"] == "plan_review"
    assert any(event["type"] == "agent_plan" for event in task["events"])

    task = engine.act(task["id"], "answer", "approve")
    assert task["stage"] == "execution_ready"
    assert task["status"] == "waiting_for_execution"
    assert task["execution_queue"][0]["action"]["phone_number"] == "+12025550199"
    assert not any(event["phase"] == "calling" for event in task["events"])

    restored = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")
    assert restored.get(task["id"])["approved_plan"] == task["approved_plan"]


def test_phone_plan_collects_caller_name_once_and_preserves_it_across_revisions(tmp_path):
    class CallerNamePlanner:
        ready = True
        model = "test"

        def __init__(self):
            self.calls = []

        def plan(self, goal, messages, contexts):
            self.calls.append(messages)
            user_supplied_name = any("Call me Mina" in message["content"] for message in messages)
            name_already_confirmed = any("caller display name is already confirmed" in message["content"] for message in messages)
            return PlanningTurn(
                status="plan_ready",
                message="The call plan is ready.",
                plan_summary="Call the provider.",
                caller_name="mina" if user_supplied_name and not name_already_confirmed else "",
                actions=[
                    PlanAction(
                        kind="phone_call",
                        label="Call provider",
                        purpose="Ask about service.",
                        target="Example Provider",
                        needs_lookup=False,
                        phone_number="+12025550199",
                        contact_provided_by="research",
                        contact_source_url="https://example.com/contact",
                    )
                ],
            )

    planner = CallerNamePlanner()
    store = SQLiteTaskStore(tmp_path / "relay.db")
    engine = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")

    task = engine.create("Call the provider about service.")
    assert task["stage"] == "collecting_context"
    assert "how should i introduce you" in task["prompt"]["question"].lower()
    assert task["caller_name"] == ""

    task = engine.act(task["id"], "instruction", "Call me Mina.")
    assert task["stage"] == "plan_review"
    assert task["caller_name"] == "Mina"

    task = engine.act(task["id"], "answer", "hold")
    task = engine.act(task["id"], "instruction", "Make the call purpose more concise.")
    assert task["stage"] == "plan_review"
    assert task["caller_name"] == "Mina"
    assert any("caller display name is already confirmed" in message["content"] for message in planner.calls[-1])
    name_questions = [
        event
        for event in task["events"]
        if event.get("speaker") == "relay_private" and "how should i introduce you" in event.get("text", "").lower()
    ]
    assert len(name_questions) == 1

    task = engine.act(task["id"], "answer", "approve")
    assert engine.call_context(task["id"], 0)["caller_name"] == "Mina"

    restored = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")
    assert restored.get(task["id"])["caller_name"] == "Mina"


def test_agentic_call_state_and_transcript_return_to_private_review(tmp_path):
    store = SQLiteTaskStore(tmp_path / "relay.db")
    planner = FakePlanner()
    engine = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")

    pending = engine.next_phone_action(task["id"])
    task = engine.begin_call(task["id"], pending["index"], "CA123")
    assert task["phase"] == "calling"
    engine.mark_call_connected(task["id"])
    engine.append_transcript(task["id"], "relay", "Hello, I am PingMeWhen, an AI tool speaking for my user.")
    engine.append_transcript(task["id"], "representative", "I am comfortable continuing.")
    task = engine.finish_call(task["id"], "CA123", "completed")

    assert task["phase"] == "planning"
    assert task["stage"] == "post_call_review"
    assert any(event.get("speaker") == "representative" for event in task["events"])
    summary = next(event for event in task["events"] if event["type"] == "call_summary")
    assert summary["target"] == "Example Provider"
    assert summary["outcome"] == "Call completed"
    assert summary["highlights"] == ["I am comfortable continuing."]
    assert task["prompt"]["options"] == []


def test_live_user_input_request_populates_prompt_and_answer_resumes_call(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    engine.mark_call_connected(task["id"])

    waiting = engine.request_user_input(
        task["id"],
        "What is your apartment number?",
        "text",
        True,
    )

    assert waiting["call_state"] == "WAITING_FOR_USER"
    assert waiting["stage"] == "waiting_for_user"
    assert waiting["prompt"]["kind"] == "text_reply"
    assert waiting["prompt"]["question"] == "What is your apartment number?"
    assert waiting["prompt"]["options"] == []
    assert waiting["prompt"]["blocking"] is True
    assert waiting["prompt"]["input_kind"] == "text"
    assert waiting["prompt"]["response_action"] == "instruction"

    resumed = engine.act(task["id"], "instruction", "Apartment 4B")

    assert resumed["call_state"] == "CONNECTED"
    assert resumed["stage"] == "calling"
    assert resumed["status"] == "running"
    assert resumed["prompt"] is None
    assert resumed["events"][-2]["speaker"] == "user_private"
    assert resumed["events"][-2]["text"] == "Apartment 4B"


def test_private_call_routing_keeps_meta_private_and_persists_confirmed_updates(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    engine.mark_call_connected(task["id"])
    engine.request_user_input(task["id"], "What is your apartment number?", "text", True)

    private = engine.record_call_private_exchange(
        task["id"],
        "Who are you?",
        "private_meta",
        None,
        "I am PingMeWhen, your private call assistant.",
        False,
    )
    assert private["call_state"] == "WAITING_FOR_USER"
    assert private["prompt"]["question"] == "What is your apartment number?"
    assert private["events"][-2]["speaker"] == "relay_private"
    assert all(
        event["channel"] == "private"
        for event in private["events"][-3:]
    )

    update = {
        "id": "update-1",
        "kind": "fact",
        "key": "apartment_number",
        "value": "4B",
        "summary": "The apartment number is 4B.",
    }
    resumed = engine.record_call_private_exchange(
        task["id"], "It is 4B.", "answer", update, "", True
    )
    assert resumed["call_state"] == "CONNECTED"
    assert resumed["prompt"] is None
    assert resumed["context_updates"] == [update]
    assert engine.call_context(task["id"], pending["index"])["context_updates"] == [update]


def test_failed_private_answer_keeps_pending_prompt_and_requests_retry(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    engine.mark_call_connected(task["id"])
    engine.request_user_input(task["id"], "What installation date works?", "text", True)

    failed = engine.record_call_delivery_failure(task["id"], "Aug 1")

    assert failed["call_state"] == "WAITING_FOR_USER"
    assert failed["prompt"]["question"] == "What installation date works?"
    assert failed["events"][-2]["speaker"] == "relay_private"
    assert "Please try again" in failed["events"][-2]["text"]
    assert failed["events"][-1]["text"] == "Answer not applied · the call remains active"


def test_user_hangup_completes_the_call_even_while_waiting_for_an_answer(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    engine.mark_call_connected(task["id"])
    engine.request_user_input(task["id"], "What installation date works?", "text", True)

    assert engine.call_sid_for(task["id"]) == "CA123"
    ended = engine.hang_up_call_by_user(task["id"])

    # A deliberate hangup resolves cleanly as completed rather than the "ended while waiting" failure path.
    assert ended["call_state"] == "COMPLETED"
    assert ended["current_call"] is None
    assert ended["phase"] == "planning"
    assert ended["stage"] == "post_call_review"


def test_hang_up_requires_an_active_call(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")

    with pytest.raises(InvalidAction):
        engine.hang_up_call_by_user(task["id"])


def test_user_authority_interaction_is_persisted_and_resolved_by_explicit_answer(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    engine.mark_call_connected(task["id"])
    waiting = engine.request_user_input(
        task["id"],
        "Alex offered $90 per month.\n\nAccept, counter, or decline?",
        "text",
        True,
        "interaction-1",
        "decision",
        "Alex offered $90 per month.",
    )
    update = {
        "id": "update-1",
        "interaction_id": "interaction-1",
        "kind": "decision",
        "key": "offer_decision",
        "value": "accept",
        "summary": "Jack approved the $90 monthly offer.",
    }

    resolved = engine.record_call_private_exchange(
        task["id"],
        "Accept",
        "answer",
        update,
        "",
        True,
        "interaction-1",
    )

    assert waiting["prompt"]["interaction_id"] == "interaction-1"
    assert waiting["pending_interactions"][0]["status"] == "pending"
    interaction = resolved["pending_interactions"][0]
    assert interaction["status"] == "resolved"
    assert interaction["resolution"] == "Accept"
    assert interaction["context_update_id"] == "update-1"


def test_completed_twilio_call_without_media_connection_is_reported_as_failed(tmp_path):
    store = SQLiteTaskStore(tmp_path / "relay.db")
    engine = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), FakePlanner(), store, lambda _: "")
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    task = engine.begin_call(task["id"], pending["index"], "CA123")

    task = engine.finish_call(task["id"], "CA123", "completed")

    assert task["stage"] == "execution_failed"
    assert task["call_state"] == "FAILED"
    assert task["execution_queue"][0]["status"] == "failed"
    assert not any("calls are complete" in event.get("text", "") for event in task["events"])
    assert any("No conversation transcript was captured" in event.get("text", "") for event in task["events"])


def test_call_ending_while_waiting_is_connected_but_incomplete(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    engine.mark_call_connected(task["id"])
    engine.append_transcript(task["id"], "representative", "What is the apartment number?")
    engine.request_user_input(task["id"], "What is your apartment number?", "text", True)

    ended = engine.finish_call(task["id"], "CA123", "completed")

    assert ended["stage"] == "execution_failed"
    assert ended["execution_queue"][0]["status"] == "failed"
    status = next(event for event in reversed(ended["events"]) if event["type"] == "status")
    assert "waiting for your answer" in status["text"]
    assert "never connected" not in status["text"]
    assert any("retained transcript" in event.get("text", "") for event in ended["events"])


def test_phone_action_requires_concrete_known_facts_in_purpose():
    with pytest.raises(ValueError, match="inline concrete known facts"):
        PlanAction(
            kind="phone_call",
            label="Call Verizon",
            purpose="Discuss internet service at the provided Boston address.",
            target="Alex at Verizon",
            needs_lookup=False,
            phone_number="+12027010927",
            contact_provided_by="user",
            contact_source_url="",
        )

    action = PlanAction(
        kind="phone_call",
        label="Call Verizon",
        purpose="Discuss internet service at 1079 Commonwealth Ave, Boston, MA 02215.",
        target="Alex at Verizon",
        known_facts=["Installation address: 1079 Commonwealth Ave, Boston, MA 02215"],
        needs_lookup=False,
        phone_number="+12027010927",
        contact_provided_by="user",
        contact_source_url="",
    )

    assert "1079 Commonwealth Ave" in action.purpose
    assert "1079 Commonwealth Ave" in action.known_facts[0]


def test_phone_action_purpose_rejects_routing_metadata_and_relay_directives():
    base = {
        "kind": "phone_call",
        "label": "Call Verizon",
        "target": "Alex at Verizon",
        "known_facts": ["Alex's reference phone number is +1 202-701-0927."],
        "needs_lookup": False,
        "phone_number": "+12027010927",
        "contact_provided_by": "user",
        "contact_source_url": "",
    }

    with pytest.raises(ValueError, match="must not duplicate"):
        PlanAction(
            **base,
            purpose="Negotiate internet installation pricing by calling +1 202-701-0927.",
        )
    with pytest.raises(ValueError, match="rather than instruct PingMeWhen"):
        PlanAction(
            **base,
            purpose="请致电 Verizon 联系人 Alex，并协助安排网络安装。",
        )

    action = PlanAction(
        **base,
        purpose=(
            "Negotiation of internet-only installation pricing at 1079 Commonwealth Ave, Boston, MA 02215, "
            "followed by enrollment and installation scheduling if the price is acceptable."
        ),
    )
    assert action.phone_number not in action.purpose
    assert action.phone_number in action.known_facts[0].replace(" ", "").replace("-", "")


def test_approval_refuses_an_unsourced_phone_action(tmp_path):
    class UnsourcedPlanner:
        ready = True
        model = "test"

        def plan(self, goal, messages, contexts):
            return PlanningTurn(
                status="plan_ready",
                message="Review this plan.",
                plan_summary="Call an unresolved contact.",
                caller_name="Taylor",
                actions=[
                    PlanAction(
                        kind="phone_call",
                        label="Call provider",
                        purpose="Ask a question.",
                        target="Provider",
                        needs_lookup=True,
                        phone_number="",
                        contact_provided_by="research",
                        contact_source_url="",
                    )
                ],
            )

    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        UnsourcedPlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider.")
    task = engine.act(task["id"], "answer", "approve")

    assert task["stage"] == "execution_blocked"
    assert task["execution_queue"] == []


def test_user_provided_contact_without_source_url_is_executable(tmp_path):
    class UserContactPlanner:
        ready = True
        model = "test"

        def plan(self, goal, messages, contexts):
            return PlanningTurn(
                status="plan_ready",
                message="Review this plan.",
                plan_summary="Call the user's personal contact.",
                caller_name="Taylor",
                actions=[
                    PlanAction(
                        kind="phone_call",
                        label="Call Alex",
                        purpose="Ask about the requested service.",
                        target="Alex",
                        needs_lookup=False,
                        phone_number="+12027010927",
                        contact_provided_by="user",
                        contact_source_url="",
                    )
                ],
            )

    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        UserContactPlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call my contact Alex at +12027010927.")
    task = engine.act(task["id"], "answer", "approve")

    assert task["stage"] == "execution_ready"
    assert task["execution_queue"][0]["action"]["contact_provided_by"] == "user"
    assert task["execution_queue"][0]["action"]["contact_source_url"] == ""


def test_researched_contact_without_source_url_remains_blocked(tmp_path):
    class UnverifiedResearchPlanner:
        ready = True
        model = "test"

        def plan(self, goal, messages, contexts):
            return PlanningTurn(
                status="plan_ready",
                message="Review this plan.",
                plan_summary="Call a researched service number.",
                caller_name="Taylor",
                actions=[
                    PlanAction(
                        kind="phone_call",
                        label="Call provider",
                        purpose="Ask about service.",
                        target="Example Provider",
                        needs_lookup=False,
                        phone_number="+12025550199",
                        contact_provided_by="research",
                        contact_source_url="",
                    )
                ],
            )

    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        UnverifiedResearchPlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call Example Provider.")
    task = engine.act(task["id"], "answer", "approve")
    status = next(event for event in reversed(task["events"]) if event["type"] == "status")

    assert task["stage"] == "execution_blocked"
    assert task["execution_queue"] == []
    assert "official contact source URL is missing or invalid" in status["text"]


def test_approval_normalizes_human_formatted_phone_number_to_e164(tmp_path):
    class FormattedNumberPlanner:
        ready = True
        model = "test"

        def plan(self, goal, messages, contexts):
            return PlanningTurn(
                status="plan_ready",
                message="Review this plan.",
                plan_summary="Call the provider.",
                caller_name="Taylor",
                actions=[
                    PlanAction(
                        kind="phone_call",
                        label="Call policy service",
                        purpose="Ask about the policy.",
                        target="Example Insurance",
                        needs_lookup=False,
                        phone_number="+1 (202) 701-0927",
                        contact_provided_by="research",
                        contact_source_url="https://example.com/contact",
                    )
                ],
            )

    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FormattedNumberPlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call Example Insurance.")
    task = engine.act(task["id"], "answer", "approve")

    assert task["stage"] == "execution_ready"
    assert task["approved_plan"]["actions"][0]["phone_number"] == "+12027010927"
    assert task["execution_queue"][0]["action"]["phone_number"] == "+12027010927"


def test_malformed_phone_number_block_names_the_offending_action(tmp_path):
    class MalformedNumberPlanner:
        ready = True
        model = "test"

        def plan(self, goal, messages, contexts):
            return PlanningTurn(
                status="plan_ready",
                message="Review this plan.",
                plan_summary="Call the provider.",
                caller_name="Taylor",
                actions=[
                    PlanAction(
                        kind="phone_call",
                        label="Call billing support",
                        purpose="Resolve a billing question.",
                        target="Example Telecom",
                        needs_lookup=False,
                        phone_number="202-CALL-NOW",
                        contact_provided_by="research",
                        contact_source_url="https://example.com/contact",
                    )
                ],
            )

    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        MalformedNumberPlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call billing support.")
    task = engine.act(task["id"], "answer", "approve")
    status = next(event for event in reversed(task["events"]) if event["type"] == "status")
    reply = next(
        event
        for event in reversed(task["events"])
        if event["type"] == "message" and event.get("speaker") == "relay_private"
    )

    assert task["stage"] == "execution_blocked"
    assert task["execution_queue"] == []
    assert "Call billing support (Example Telecom)" in status["text"]
    assert 'phone number "202-CALL-NOW" does not resolve to valid E.164' in status["text"]
    assert "Call billing support (Example Telecom)" in reply["text"]


def test_sensitive_request_requires_typed_takeover_without_exposing_a_value(tmp_path):
    log_path = tmp_path / "events.jsonl"
    engine = AgenticTaskEngine(
        EventLog(log_path),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    task = engine.begin_call(task["id"], pending["index"], "CA123")

    task = engine.request_secure_field(task["id"], "card_number")
    assert task["call_state"] == "HUMAN_TAKEOVER"
    assert task["stage"] == "takeover_required"
    assert task["takeover_active"] is False
    assert task["takeover_sensitive"] is True
    assert task["prompt"]["kind"] == "takeover_required"
    assert "fake_value" not in task["prompt"]
    event_count = len(task["events"])
    engine.append_transcript(task["id"], "representative", "4242 4242 4242 4242")
    assert len(engine.get(task["id"])["events"]) == event_count

    task = engine.begin_typed_takeover(task["id"], sensitive=True)
    assert task["call_state"] == "HUMAN_TAKEOVER"
    assert task["stage"] == "human_takeover"
    assert task["takeover_active"] is True
    assert task["secure_mode"] is True
    assert "4242" not in log_path.read_text()

    task = engine.resume_from_takeover(task["id"])
    assert task["call_state"] == "CONNECTED"
    assert task["secure_mode"] is False
    assert task["takeover_active"] is False
    assert "card_number" in task["secure_fields_completed"]


def test_sensitive_ssn_and_date_of_birth_prompts_use_protected_input_metadata(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")

    ssn = engine.request_secure_field(task["id"], "ssn_last_four")
    assert ssn["prompt"]["input_kind"] == "masked_numeric"
    assert ssn["prompt"]["max_length"] == 4

    engine.begin_typed_takeover(task["id"], sensitive=True)
    engine.resume_from_takeover(task["id"])
    dob = engine.request_secure_field(task["id"], "date_of_birth")
    assert dob["prompt"]["input_kind"] == "date"
    assert dob["prompt"]["field"] == "date_of_birth"


def test_human_takeover_can_explicitly_resume_the_active_call(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")
    task = engine.request_secure_field(task["id"], "verification_request")
    task = engine.begin_typed_takeover(task["id"], sensitive=True)

    resumed = engine.resume_from_takeover(task["id"])

    assert task["call_state"] == "HUMAN_TAKEOVER"
    assert resumed["call_state"] == "CONNECTED"
    assert resumed["stage"] == "calling"
    assert resumed["status"] == "running"
    assert resumed["secure_mode"] is False
    assert resumed["takeover_active"] is False
    assert resumed["secure_expected_field"] is None
    assert resumed["prompt"] is None
    assert resumed["events"][-1]["text"] == "Human takeover ended · PingMeWhen returned to the active call"


def test_general_typed_takeover_records_state_and_context_on_resume(tmp_path):
    log_path = tmp_path / "events.jsonl"
    engine = AgenticTaskEngine(
        EventLog(log_path),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")

    active = engine.begin_typed_takeover(task["id"])
    assert active["call_state"] == "HUMAN_TAKEOVER"
    assert active["takeover_active"] is True
    assert active["takeover_sensitive"] is False

    update = {
        "id": "update-1",
        "kind": "takeover_context",
        "key": "typed_takeover_exchange",
        "value": "Tuesday works.",
        "summary": "The represented person accepted Tuesday.",
    }
    resumed = engine.resume_from_takeover(task["id"], update)

    assert resumed["call_state"] == "CONNECTED"
    assert resumed["context_updates"][-1] == update
    events = log_path.read_text()
    assert "call.takeover_started" in events
    assert "call.takeover_ended" in events


def test_resume_from_takeover_rejects_any_other_call_state(tmp_path):
    engine = AgenticTaskEngine(
        EventLog(tmp_path / "events.jsonl"),
        FakePlanner(),
        SQLiteTaskStore(tmp_path / "relay.db"),
        lambda _: "",
    )
    task = engine.create("Call a provider using 123 Main Street, Washington, DC 20001.")
    task = engine.act(task["id"], "answer", "approve")
    pending = engine.next_phone_action(task["id"])
    engine.begin_call(task["id"], pending["index"], "CA123")

    with pytest.raises(InvalidAction, match="only after human takeover"):
        engine.resume_from_takeover(task["id"])


def test_production_app_reports_planner_and_persists_state(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MODE", "standard")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12025550123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = TestClient(create_app(planner=FakePlanner()))

    runtime = client.get("/api/runtime").json()
    assert runtime["workflow"] == "agentic-private-planning"
    assert runtime["planner_ready"] is True
    assert runtime["planner_model"] == "fake-planner"

    created = client.post("/api/tasks", json={"goal": "Arrange a service call."})
    assert created.status_code == 200
    assert (tmp_path / "state" / "relay.db").exists()


def test_production_app_returns_clear_error_without_backend_credential(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MODE", "standard")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12025550123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = TestClient(create_app(planner=UnavailablePlanner()))

    response = client.post("/api/tasks", json={"goal": "Arrange a service call."})

    assert response.status_code == 503
    assert "OpenAI API key" in response.json()["detail"]


def test_failed_replan_does_not_corrupt_persisted_task(tmp_path):
    class FailingAfterFirstPlan(FakePlanner):
        def plan(self, goal, messages, contexts):
            if self.calls:
                raise PlannerError("temporary failure")
            self.calls += 1
            return PlanningTurn(
                status="needs_input",
                message="I need one detail.",
                plan_summary="",
                questions=["Which provider should PingMeWhen contact?"],
            )

    store = SQLiteTaskStore(tmp_path / "relay.db")
    planner = FailingAfterFirstPlan()
    engine = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")
    task = engine.create("Arrange a service call.")

    try:
        engine.act(task["id"], "instruction", "Use Provider A.")
    except PlannerError:
        pass

    assert engine.get(task["id"]) == task
    restored = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")
    assert restored.get(task["id"]) == task
