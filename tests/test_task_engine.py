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


def test_non_address_reply_is_not_silently_accepted(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Get renters insurance quotes.")

    task = act(engine, task, "instruction", "oh I don't have it")

    assert task["stage"] == "plan_address"
    assert task["address"] is None
    assert not any(event["type"] == "plan" for event in task["events"])
    assert "does not look like" in task["events"][-1]["text"]


def test_pdf_attached_during_planning_requires_address_confirmation(tmp_path):
    engine = make_engine(tmp_path)
    task = engine.create("Get renters insurance quotes.")

    task = engine.attach_context(
        task["id"],
        {
            "id": "context-1",
            "filename": "lease.pdf",
            "pages": 2,
            "characters": 300,
            "address_candidate": "123 Main Street, Washington, DC 20001",
        },
    )

    assert task["stage"] == "confirm_address"
    assert task["prompt"]["options"][0]["value"] == "use_address"
    task = answer(engine, task, "use_address")
    assert task["stage"] == "plan_review"
    assert task["address"] == "123 Main Street, Washington, DC 20001"


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
    assert task["phase"] == "planning"
    assert len(task["quotes"]) == 3
    assert any(event["type"] == "comparison" for event in task["events"])

    task = answer(engine, task, "cedar")
    task = answer(engine, task, "approve")
    assert task["phase"] == "calling"
    task = advance_until_waiting(engine, task)
    assert task["stage"] == "confirm_application"

    task = answer(engine, task, "confirm")
    task = advance_until_waiting(engine, task)
    assert task["stage"] == "payment_method"

    task = answer(engine, task, "local_tts")
    assert task["secure_mode"] is True
    assert task["stage"] == "secure_card_number"
    assert task["prompt"]["field"] == "card_number"
    task = answer(engine, task, "sent")
    assert task["stage"] == "secure_expiration"
    assert task["prompt"]["field"] == "expiration"
    task = answer(engine, task, "sent")
    assert task["stage"] == "secure_cvv"
    assert task["prompt"]["field"] == "cvv"
    task = answer(engine, task, "sent")
    assert task["status"] == "complete"
    assert task["secure_mode"] is False
    assert task["events"][-1]["phase"] == "planning"


def test_each_new_representative_gets_a_fresh_introduction_and_call_context(tmp_path):
    engine = make_engine(tmp_path)
    task = reach_comparison(engine, begin_calls(engine, engine.create("Collect three quotes.")))

    call_events = [event for event in task["events"] if event["phase"] == "calling"]
    relay_introductions = [
        event
        for event in call_events
        if event["type"] == "message"
        and event["speaker"] == "relay"
        and "AI voice assistant speaking for Alex" in event["text"]
    ]
    assert len(relay_introductions) == 3
    for insurer in ("Northstar Insurance", "Cedar Shield"):
        greeting_index = next(
            index
            for index, event in enumerate(call_events)
            if event.get("company") == insurer and "How can I help" in event.get("text", "")
        )
        quote_index = next(
            index
            for index, event in enumerate(call_events)
            if event.get("company") == insurer and "quote is" in event.get("text", "")
        )
        between = call_events[greeting_index:quote_index]
        assert any(
            event.get("speaker") == "relay" and "property is" in event.get("text", "").lower()
            for event in between
        )


def test_application_callback_starts_a_new_representative_conversation(tmp_path):
    engine = make_engine(tmp_path)
    task = reach_comparison(engine, begin_calls(engine, engine.create("Collect three quotes.")))
    task = answer(engine, task, "cedar")
    task = advance_until_waiting(engine, answer(engine, task, "approve"))

    callback_start = next(
        index
        for index, event in enumerate(task["events"])
        if event["type"] == "status" and "continuing simulated application" in event.get("text", "")
    )
    callback_events = task["events"][callback_start:]
    assert callback_events[1]["speaker"] == "representative"
    assert "How can I help?" in callback_events[1]["text"]
    assert callback_events[2]["speaker"] == "relay"
    assert "AI voice assistant speaking for Alex" in callback_events[2]["text"]
    assert "previously received a renters quote" in callback_events[2]["text"]


def test_local_tts_cycles_one_field_at_a_time_without_logging_values(tmp_path):
    log_path = tmp_path / "events.jsonl"
    engine = DeterministicTaskEngine(EventLog(log_path))
    task = reach_comparison(engine, begin_calls(engine, engine.create("Collect quotes.")))
    task = answer(engine, task, "harbor")
    task = advance_until_waiting(engine, answer(engine, task, "approve"))
    task = advance_until_waiting(engine, answer(engine, task, "confirm"))
    task = answer(engine, task, "local_tts")

    expected = [
        ("secure_card_number", "card_number"),
        ("secure_expiration", "expiration"),
        ("secure_cvv", "cvv"),
    ]
    for stage, field in expected:
        assert task["stage"] == stage
        assert task["prompt"]["kind"] == "secure_field"
        assert task["prompt"]["field"] == field
        assert task["prompt"]["options"] == []
        task = answer(engine, task, "sent")

    logged = log_path.read_text()
    assert "4242424242424242" not in logged
    assert '\"value\":\"sent\"' in logged


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
    assert "no microphone or telephone audio" in task["events"][-1]["text"]

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
