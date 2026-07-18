import asyncio
from types import SimpleNamespace

from relay_agent.gatekeeper import (
    AuthorityVeto,
    ContextUpdate,
    GatekeeperVerdict,
    OpenAIGatekeeper,
    PrivateMessageRequest,
    PrivateMessageRoute,
    gatekeeper_request,
)


class ClassifyingResponses:
    def __init__(self):
        self.calls = []

    def parse(self, **arguments):
        self.calls.append(arguments)
        latest = arguments["input"][-1]["content"]
        if "What is the apartment number?" in latest and "Apartment 4B" not in latest:
            verdict = GatekeeperVerdict(
                route="consult_user",
                reason="missing_fact",
                representative_update="The representative asked for the apartment number.",
                question_to_user="What is your apartment number?",
            )
        else:
            verdict = GatekeeperVerdict(route="continue", reason="none")
        return SimpleNamespace(output_parsed=verdict)


def test_gatekeeper_classifies_against_context_and_accumulated_user_updates():
    responses = ClassifyingResponses()
    client = SimpleNamespace(responses=responses)
    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: client,
    )
    context = {
        "goal": "Arrange internet service.",
        "action": {"purpose": "Install at 1079 Commonwealth Ave.", "target": "Alex at Verizon"},
    }

    missing = asyncio.run(
        gatekeeper.classify(gatekeeper_request("What is the apartment number?", context, []))
    )
    known = asyncio.run(
        gatekeeper.classify(
            gatekeeper_request(
                "What is the apartment number?",
                context,
                [{"kind": "fact", "key": "apartment_number", "value": "Apartment 4B"}],
            )
        )
    )

    assert missing == GatekeeperVerdict(
        route="consult_user",
        reason="missing_fact",
        representative_update="The representative asked for the apartment number.",
        question_to_user="What is your apartment number?",
    )
    assert known == GatekeeperVerdict(route="continue", reason="none")
    assert responses.calls[0]["model"] == "gpt-5.4-nano"
    assert responses.calls[0]["reasoning"] == {"effort": "none"}
    assert responses.calls[0]["text_format"] is GatekeeperVerdict
    assert "1079 Commonwealth Ave" in responses.calls[0]["input"][-1]["content"]
    assert "Apartment 4B" in responses.calls[1]["input"][-1]["content"]


def test_gatekeeper_schema_surfaces_material_offer_for_explicit_user_decision():
    verdict = GatekeeperVerdict(
        route="consult_user",
        reason="decision",
        representative_update=(
            "Alex offered internet service for $100 per month if Jack enrolls with a contract device."
        ),
        question_to_user="Do you want Relay to accept, counter, or decline?",
    )
    request = gatekeeper_request(
        "I can do $100, but only if you enroll with a contract device.",
        {"approved_call": {"known_facts": ["Monthly budget: $90"]}},
        [],
    )

    assert verdict.route == "consult_user"
    assert "$100" in verdict.representative_update
    instructions = request.instructions
    assert "USER AUTHORITY RULE" in instructions
    assert "constraint, not permission" in instructions
    assert "Never ask the user to supply a fact the representative just supplied" in instructions


def test_authority_veto_uses_a_separate_fail_closed_schema():
    class VetoResponses:
        def parse(self, **arguments):
            return SimpleNamespace(
                output_parsed=AuthorityVeto(
                    requires_user=True,
                    reason="approval",
                    representative_update="The representative is ready to submit enrollment.",
                    question_to_user="Do you approve submitting it?",
                )
            )

    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: SimpleNamespace(responses=VetoResponses()),
    )
    veto = asyncio.run(gatekeeper.veto(gatekeeper_request("Should I submit it?", {}, [])))

    assert veto.requires_user is True
    assert veto.reason == "approval"


def test_gatekeeper_routes_private_meta_without_speaker_update_and_answers_with_one():
    class RoutingResponses:
        def __init__(self):
            self.calls = []

        def parse(self, **arguments):
            self.calls.append(arguments)
            content = arguments["input"][-1]["content"]
            if "Who are you?" in content:
                route = PrivateMessageRoute(
                    disposition="private_meta",
                    private_reply="I am Relay, your private call assistant.",
                )
            else:
                route = PrivateMessageRoute(
                    disposition="answer",
                    speaker_update=ContextUpdate(
                        kind="fact",
                        key="apartment_number",
                        value="4B",
                        summary="The apartment number is 4B.",
                    ),
                )
            return SimpleNamespace(output_parsed=route)

    responses = RoutingResponses()
    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: SimpleNamespace(responses=responses),
    )
    base = dict(context={}, context_updates=(), waiting_for_user=True, pending_question="Apartment number?")

    meta = asyncio.run(gatekeeper.route_private_message(PrivateMessageRequest(text="Who are you?", **base)))
    answer = asyncio.run(gatekeeper.route_private_message(PrivateMessageRequest(text="It is 4B.", **base)))

    assert meta.disposition == "private_meta"
    assert meta.speaker_update is None
    assert answer.speaker_update == ContextUpdate(
        kind="fact",
        key="apartment_number",
        value="It is 4B.",
        summary="The apartment number is 4B.",
    )
    assert responses.calls[0]["text_format"] is PrivateMessageRoute


def test_private_message_prompt_presumes_terse_reply_answers_pending_question():
    request = PrivateMessageRequest(
        text="7A",
        context={},
        context_updates=(),
        waiting_for_user=True,
        pending_question="What is the apartment number?",
    )

    instructions = request.messages()[0]["content"]

    assert "strongly presume" in instructions
    assert "Short or terse replies such as '7A'" in instructions
    assert "Brevity alone is never a reason" in instructions
    assert "Reserve private_meta for genuinely off-topic or meta conversation" in instructions


def test_pending_authority_answer_is_forced_to_a_typed_decision_update():
    class DecisionResponses:
        def parse(self, **arguments):
            return SimpleNamespace(
                output_parsed=PrivateMessageRoute(
                    disposition="answer",
                    speaker_update=ContextUpdate(
                        kind="fact",
                        key="offer_response",
                        value="accept",
                        summary="The represented person accepts the current offer.",
                    ),
                )
            )

    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: SimpleNamespace(responses=DecisionResponses()),
    )
    request = PrivateMessageRequest(
        text="Accept",
        context={},
        context_updates=(),
        waiting_for_user=True,
        pending_question="Accept, counter, or decline?",
        pending_reason="decision",
        pending_interaction_id="interaction-1",
    )

    route = asyncio.run(gatekeeper.route_private_message(request))

    assert route.speaker_update.kind == "decision"
    assert "PENDING INTERACTION ID: interaction-1" in request.messages()[1]["content"]


def test_gatekeeper_repairs_answer_that_omits_speaker_update():
    class IncompleteRoutingResponses:
        def parse(self, **arguments):
            return SimpleNamespace(
                output_parsed=PrivateMessageRoute(
                    disposition="answer",
                    private_reply="The apartment number is still missing.",
                )
            )

    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: SimpleNamespace(responses=IncompleteRoutingResponses()),
    )
    request = PrivateMessageRequest(
        text="30C",
        context={},
        context_updates=(),
        waiting_for_user=True,
        pending_question="What is the apartment or unit number?",
    )

    route = asyncio.run(gatekeeper.route_private_message(request))

    assert route.disposition == "answer"
    assert route.private_reply == ""
    assert route.speaker_update == ContextUpdate(
        kind="fact",
        key="apartment_number",
        value="30C",
        summary="The apartment/unit number is 30C.",
    )


def test_late_retry_marked_as_answer_becomes_context_update_instead_of_failing():
    class RetryRoutingResponses:
        def parse(self, **arguments):
            return SimpleNamespace(
                output_parsed=PrivateMessageRoute(
                    disposition="answer",
                    speaker_update=ContextUpdate(
                        kind="fact",
                        key="installation_date",
                        value="Aug 1",
                        summary="The preferred installation date is Aug 1.",
                    ),
                )
            )

    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: SimpleNamespace(responses=RetryRoutingResponses()),
    )
    request = PrivateMessageRequest(
        text="Aug 1st",
        context={},
        context_updates=(),
        waiting_for_user=False,
        pending_question="",
    )

    route = asyncio.run(gatekeeper.route_private_message(request))

    assert route.disposition == "context_update"
    assert route.speaker_update.key == "installation_date"


def test_private_answer_prompt_requires_question_specific_context_update():
    request = PrivateMessageRequest(
        text="2711",
        context={},
        context_updates=(),
        waiting_for_user=True,
        pending_question="What is the apartment number?",
    )

    instructions = request.messages()[0]["content"]

    assert "stable, semantically specific key from PENDING QUESTION" in instructions
    assert "apartment_number" in instructions
    assert "Never use a generic key such as pending_question_answer" in instructions


def test_gatekeeper_keeps_private_meta_isolated_when_model_returns_an_update():
    class UnsafeMetaRoutingResponses:
        def parse(self, **arguments):
            return SimpleNamespace(
                output_parsed=PrivateMessageRoute(
                    disposition="private_meta",
                    speaker_update=ContextUpdate(
                        kind="fact",
                        key="private_message",
                        value="Who are you?",
                        summary="The represented person asked who Relay is.",
                    ),
                )
            )

    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=lambda **kwargs: SimpleNamespace(responses=UnsafeMetaRoutingResponses()),
    )
    request = PrivateMessageRequest(
        text="Who are you?",
        context={},
        context_updates=(),
        waiting_for_user=False,
        pending_question="",
    )

    route = asyncio.run(gatekeeper.route_private_message(request))

    assert route.disposition == "private_meta"
    assert route.speaker_update is None
    assert route.private_reply == "I kept that message in our private workspace."


def test_gatekeeper_reuses_the_client_for_the_same_api_key():
    clients = []

    def client_factory(**kwargs):
        client = SimpleNamespace(responses=ClassifyingResponses())
        clients.append((kwargs, client))
        return client

    gatekeeper = OpenAIGatekeeper(
        lambda: "sk-test",
        lambda: "gpt-5.4-nano",
        client_factory=client_factory,
    )
    request = gatekeeper_request("What plans are available?", {}, [])

    asyncio.run(gatekeeper.classify(request))
    asyncio.run(gatekeeper.classify(request))

    assert len(clients) == 1


def test_gatekeeper_request_carries_an_explicit_identity_block():
    request = gatekeeper_request(
        "The monthly charge is $150.",
        {"approved_call": {"target": "Alex / Verizon"}},
        [],
        represented_user="Ryan",
        representative_name="Alex / Verizon",
    )

    content = request.messages()[1]["content"]
    assert "Represented user (whose authority you protect; private questions go to them): Ryan" in content
    assert "Representative (who said the LATEST UTTERANCE below; Relay called them): Alex / Verizon" in content
    assert "LATEST REPRESENTATIVE UTTERANCE (said by Alex / Verizon)" in content


def test_gatekeeper_request_defaults_identity_when_names_are_unknown():
    request = gatekeeper_request("Hello?", {}, [])

    content = request.messages()[1]["content"]
    assert "the represented user" in content
    assert "the representative" in content


def test_private_message_request_carries_an_explicit_identity_block():
    request = PrivateMessageRequest(
        text="5E",
        context={},
        context_updates=(),
        waiting_for_user=True,
        pending_question="What is the apartment number?",
        represented_user="Ryan",
        representative_name="Alex / Verizon",
    )

    content = request.messages()[1]["content"]
    assert "Represented user (the PRIVATE DASHBOARD MESSAGE below is FROM this person): Ryan" in content
    assert "Representative (Relay called them; they never send dashboard messages): Alex / Verizon" in content
