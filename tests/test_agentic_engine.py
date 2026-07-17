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
                questions=["What service address should Relay use?"],
            )
        return PlanningTurn(
            status="plan_ready",
            message="I have enough information to propose a plan.",
            plan_summary="Verify three providers, call each for a standardized quote, then return the facts for your decision.",
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
    engine.append_transcript(task["id"], "relay", "Hello, I am Relay, an AI tool speaking for my user.")
    engine.append_transcript(task["id"], "representative", "I am comfortable continuing.")
    task = engine.finish_call(task["id"], "CA123", "completed")

    assert task["phase"] == "planning"
    assert task["stage"] == "post_call_review"
    assert any(event.get("speaker") == "representative" for event in task["events"])


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


def test_approval_refuses_an_unsourced_phone_action(tmp_path):
    class UnsourcedPlanner:
        ready = True
        model = "test"

        def plan(self, goal, messages, contexts):
            return PlanningTurn(
                status="plan_ready",
                message="Review this plan.",
                plan_summary="Call an unresolved contact.",
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


def test_secure_fields_cycle_individually_and_repeat_routes_to_takeover(tmp_path):
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
    assert task["call_state"] == "SECURE_LOCAL"
    assert task["prompt"]["field"] == "card_number"
    event_count = len(task["events"])
    engine.append_transcript(task["id"], "representative", "4242 4242 4242 4242")
    assert len(engine.get(task["id"])["events"]) == event_count

    task = engine.complete_secure_field(task["id"], "card_number")
    assert task["call_state"] == "CONNECTED"
    assert task["secure_mode"] is False
    task = engine.request_secure_field(task["id"], "expiration")
    assert task["prompt"]["field"] == "expiration"
    task = engine.complete_secure_field(task["id"], "expiration")

    task = engine.request_secure_field(task["id"], "card_number")
    assert task["call_state"] == "HUMAN_TAKEOVER"
    assert task["stage"] == "human_takeover"
    assert task["secure_mode"] is True
    assert "4242" not in log_path.read_text()


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

    resumed = engine.resume_from_takeover(task["id"])

    assert task["call_state"] == "HUMAN_TAKEOVER"
    assert resumed["call_state"] == "CONNECTED"
    assert resumed["stage"] == "calling"
    assert resumed["status"] == "running"
    assert resumed["secure_mode"] is False
    assert resumed["secure_expected_field"] is None
    assert resumed["prompt"] is None
    assert resumed["events"][-1]["text"] == "Human takeover ended · Relay returned to the active call"


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
                questions=["Which provider should Relay contact?"],
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
