import asyncio
from types import SimpleNamespace

from relay_agent.gatekeeper import GatekeeperVerdict, OpenAIGatekeeper, gatekeeper_request


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
            gatekeeper_request("What is the apartment number?", context, ["Apartment 4B"])
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
