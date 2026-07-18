from relay_agent.interaction_prompts import user_input_prompt


def test_yes_no_question_renders_quick_replies_with_free_text_fallback():
    prompt = user_input_prompt(
        "The representative asked whether a business operates at the address.\n\n"
        "Do you operate a business from this address?"
    )

    assert prompt["kind"] == "quick_reply"
    assert [option["value"] for option in prompt["options"]] == ["yes", "no"]
    assert prompt["allow_text"] is True
    assert prompt["response_action"] == "instruction"


def test_dates_and_identifiers_render_context_specific_inputs():
    assert user_input_prompt("What is your date of birth?")["input_kind"] == "date"
    assert user_input_prompt("Which installation date works?")["input_kind"] == "date"
    assert user_input_prompt("What is your account number?")["input_kind"] == "masked"


def test_multi_option_question_does_not_collapse_to_yes_no():
    prompt = user_input_prompt("Would you prefer Tuesday or Wednesday?")

    assert prompt["kind"] == "text_reply"
    assert prompt["options"] == []
