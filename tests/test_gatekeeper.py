import asyncio
from types import SimpleNamespace

from relay_agent.gatekeeper import (
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
                verdict="unanswerable",
                question="What is your apartment number?",
            )
        else:
            verdict = GatekeeperVerdict(verdict="answerable")
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
        verdict="unanswerable",
        question="What is your apartment number?",
    )
    assert known == GatekeeperVerdict(verdict="answerable")
    assert responses.calls[0]["model"] == "gpt-5.4-nano"
    assert responses.calls[0]["reasoning"] == {"effort": "none"}
    assert responses.calls[0]["text_format"] is GatekeeperVerdict
    assert "1079 Commonwealth Ave" in responses.calls[0]["input"][-1]["content"]
    assert "Apartment 4B" in responses.calls[1]["input"][-1]["content"]


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
        key="pending_question_answer",
        value="It is 4B.",
        summary="The represented person supplied this answer to the pending question: It is 4B.",
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
        key="pending_question_answer",
        value="30C",
        summary="The represented person supplied this answer to the pending question: 30C",
    )


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
