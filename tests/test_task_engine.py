from relay_agent.event_log import EventLog
from relay_agent.task_engine import DeterministicTaskEngine


def make_engine(tmp_path):
    return DeterministicTaskEngine(EventLog(tmp_path / "events.jsonl"))


def act(engine, task, action, value=""):
    return engine.act(task["id"], action, value)


def answer(engine, task, value):
    return act(engine, task, "answer", value)


def advance_until_waiting(engine, task):
    while task["auto_advance"]:
        task = act(engine, task, "advance")
    return task


def begin_calls(engine, task):
    task = act(engine, task, "instruction", "123 Demo Street, Washington, DC 20001")
    assert task["stage"] == "plan_review"
    task = answer(engine, task, "approve")
    assert task["phase"] == "calling"
    return task


def reach_comparison(engine, task):
    task = advance_until_waiting(engine, task)
    assert task["stage"] == "claims_history"
    task = answer(engine, task, "no")
    return advance_until_waiting(engine, task)


def test_planning_requires_clarification_and_explicit_approval(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Get three renters insurance quotes and let me choose.")

    assert task["phase"] == "planning"
    assert task["stage"] == "plan_address"
    assert "address" in task["prompt"]["question"].lower()

    task = act(engine, task, "instruction", "123 Demo Street")
    assert task["stage"] == "plan_review"
    assert task["prompt"]["options"][0]["value"] == "approve"
    plan = next(event for event in reversed(task["events"]) if event["type"] == "plan")
    assert len(plan["carriers"]) == 3
    assert all("phone" in carrier for carrier in plan["carriers"])


def test_plan_can_be_revised_for_multiple_rounds(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Collect three quotes.")
    task = act(engine, task, "instruction", "123 Demo Street")
    task = answer(engine, task, "hold")
    task = act(
        engine,
        task,
        "instruction",
        "Don't call Northstar, add Summit, and ask whether a multi-policy discount is available.",
    )

    assert "northstar" not in task["carrier_ids"]
    assert "summit" in task["carrier_ids"]
    assert any("multi-policy" in question for question in task["strategy_questions"])
    assert task["stage"] == "plan_review"


def test_complete_deterministic_insurance_workflow(tmp_path):
    engine = make_engine(tmp_path)
    task = begin_calls(engine, engine.create("Get three quotes and let me choose."))
    task = reach_comparison(engine, task)

    assert task["stage"] == "select_insurer"
    assert len(task["quotes"]) == 3
    assert any(event["type"] == "comparison" for event in task["events"])

    task = answer(engine, task, "cedar")
    task = answer(engine, task, "approve")
    task = advance_until_waiting(engine, task)
    assert task["stage"] == "confirm_application"

    task = answer(engine, task, "confirm")
    task = advance_until_waiting(engine, task)
    assert task["stage"] == "payment_method"

    task = answer(engine, task, "local_tts")
    assert task["secure_mode"] is True
    task = answer(engine, task, "complete")
    assert task["status"] == "complete"
    assert task["secure_mode"] is False


def test_user_can_stop_before_callback(tmp_path):
    engine = make_engine(tmp_path)
    task = reach_comparison(engine, begin_calls(engine, engine.create("Collect quotes.")))
    task = answer(engine, task, "harbor")
    task = answer(engine, task, "stop")

    assert task["status"] == "complete"
    assert "no purchase" in task["events"][-1]["text"]


def test_takeover_can_interrupt_script_and_resume(tmp_path):
    engine = make_engine(tmp_path)
    task = begin_calls(engine, engine.create("Collect quotes."))
    assert task["auto_advance"] is True

    task = act(engine, task, "takeover")
    assert task["stage"] == "takeover"
    assert task["auto_advance"] is False

    task = answer(engine, task, "resume")
    assert task["auto_advance"] is True
    task = advance_until_waiting(engine, task)
    assert task["stage"] == "claims_history"


def test_barge_in_is_inserted_before_next_scripted_turn(tmp_path):
    engine = make_engine(tmp_path)
    task = begin_calls(engine, engine.create("Collect quotes."))
    task = act(engine, task, "advance")
    event_count = len(task["events"])

    task = act(engine, task, "instruction", "Do they offer a multi-policy discount?")
    assert task["events"][-1]["speaker"] == "user_private"

    task = act(engine, task, "advance")
    inserted = task["events"][-1]
    assert len(task["events"]) == event_count + 2
    assert inserted["speaker"] == "relay"
    assert "multi-policy discount" in inserted["text"]


def test_risky_payment_requires_second_confirmation(tmp_path):
    engine = make_engine(tmp_path)
    task = reach_comparison(engine, begin_calls(engine, engine.create("Collect quotes.")))
    task = answer(engine, task, "northstar")
    task = advance_until_waiting(engine, answer(engine, task, "approve"))
    task = advance_until_waiting(engine, answer(engine, task, "confirm"))
    task = answer(engine, task, "risky")

    assert task["stage"] == "risky_confirmation"
    task = answer(engine, task, "cancel")
    assert task["stage"] == "payment_method"
