from fastapi.testclient import TestClient

from relay_agent.agentic_engine import AgenticTaskEngine
from relay_agent.app import create_app
from relay_agent.event_log import EventLog
from relay_agent.planner import PlanAction, PlannerError, PlanningTurn, UnavailablePlanner
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
                ),
                PlanAction(
                    kind="phone_call",
                    label="Collect standardized quotes",
                    purpose="Ask the same factual coverage and price questions.",
                    target="Three verified provider numbers",
                    needs_lookup=True,
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
    assert not any(event["phase"] == "calling" for event in task["events"])

    restored = AgenticTaskEngine(EventLog(tmp_path / "events.jsonl"), planner, store, lambda _: "")
    assert restored.get(task["id"])["approved_plan"] == task["approved_plan"]


def test_production_app_reports_planner_and_persists_state(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MODE", "standard")
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
    client = TestClient(create_app(planner=UnavailablePlanner()))

    response = client.post("/api/tasks", json={"goal": "Arrange a service call."})

    assert response.status_code == 503
    assert "backend OpenAI API credential" in response.json()["detail"]


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
