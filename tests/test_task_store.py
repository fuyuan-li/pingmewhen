from relay_agent.event_log import EventLog
from relay_agent.task_engine import DeterministicTaskEngine
from relay_agent.task_store import SQLiteTaskStore


def test_deterministic_task_resumes_from_sqlite(tmp_path):
    store = SQLiteTaskStore(tmp_path / "relay.db")
    first = DeterministicTaskEngine(EventLog(tmp_path / "events.jsonl"), store)
    task = first.create("Collect three quotes.")
    task = first.act(task["id"], "instruction", "123 Demo Street")
    task = first.act(task["id"], "answer", "approve")
    task = first.act(task["id"], "advance")

    restored = DeterministicTaskEngine(EventLog(tmp_path / "events.jsonl"), store)
    loaded = restored.get(task["id"])

    assert loaded["events"] == task["events"]
    assert loaded["auto_advance"] is True
    assert restored.act(task["id"], "advance")["events"][-1]["id"] == task["events"][-1]["id"] + 1
