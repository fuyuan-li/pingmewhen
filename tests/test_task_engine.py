from relay_agent.event_log import EventLog
from relay_agent.task_engine import DeterministicTaskEngine


def make_engine(tmp_path):
    return DeterministicTaskEngine(EventLog(tmp_path / "events.jsonl"))


def answer(engine, task, value):
    return engine.act(task["id"], "answer", value)


def test_complete_deterministic_insurance_workflow(tmp_path):
    engine = make_engine(tmp_path)

    task = engine.create("Get three renters insurance quotes and let me choose.")
    assert task["stage"] == "claims_history"
    assert task["prompt"]["options"][0]["label"] == "No"

    task = answer(engine, task, "no")
    assert task["stage"] == "select_insurer"
    assert len(task["quotes"]) == 3
    assert any(event["type"] == "comparison" for event in task["events"])

    task = answer(engine, task, "cedar")
    assert task["selected_insurer"] == "Cedar Shield"
    assert task["stage"] == "approve_callback"

    task = answer(engine, task, "approve")
    assert task["stage"] == "confirm_application"

    task = answer(engine, task, "confirm")
    assert task["stage"] == "payment_method"

    task = answer(engine, task, "local_tts")
    assert task["secure_mode"] is True
    assert task["prompt"]["kind"] == "secure_entry"

    task = answer(engine, task, "complete")
    assert task["status"] == "complete"
    assert task["secure_mode"] is False
    assert task["prompt"] is None
    assert task["events"][-1]["text"].startswith("Task complete")


def test_user_can_stop_before_callback(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Collect quotes.")
    task = answer(engine, task, "no")
    task = answer(engine, task, "harbor")
    task = answer(engine, task, "stop")

    assert task["status"] == "complete"
    assert "No purchase" in task["events"][-1]["text"]


def test_takeover_can_interrupt_and_resume(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Collect quotes.")
    original_prompt = task["prompt"]

    task = engine.act(task["id"], "takeover")
    assert task["stage"] == "takeover"
    assert "takeover active" in task["events"][-1]["text"].lower()

    task = answer(engine, task, "resume")
    assert task["stage"] == "claims_history"
    assert task["prompt"] == original_prompt


def test_risky_payment_requires_second_confirmation(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Collect quotes and continue after I choose.")
    for value in ("no", "northstar", "approve", "confirm", "risky"):
        task = answer(engine, task, value)

    assert task["stage"] == "risky_confirmation"
    assert "not recommended" in task["prompt"]["question"].lower()

    task = answer(engine, task, "cancel")
    assert task["stage"] == "payment_method"


def test_private_instruction_is_visible_but_does_not_remove_prompt(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Collect quotes.")
    prompt = task["prompt"]

    task = engine.act(task["id"], "instruction", "Ask whether bicycle theft is covered.")

    assert task["prompt"] == prompt
    assert task["events"][-2]["speaker"] == "user_private"
    assert task["events"][-2]["text"] == "Ask whether bicycle theft is covered."
