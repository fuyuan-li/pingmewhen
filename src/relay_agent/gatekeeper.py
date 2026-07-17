from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

from pydantic import BaseModel, Field


GATEKEEPER_INSTRUCTIONS = (
    "You are Relay's call Gatekeeper. Decide whether the voice Speaker can answer the representative's latest "
    "utterance without private help from the represented user. Return answerable when Speaker can respond faithfully "
    "from the supplied task context and user updates, or when no private fact, preference, decision, or authority is "
    "needed, including greetings, acknowledgements, and questions about facts the representative owns. Return "
    "unanswerable only when responding requires guessing or inventing the user's personal fact, preference, decision, "
    "or authority. For unanswerable, write one concise question to show privately to the represented user. Do not "
    "answer the representative and do not add explanations."
)


class GatekeeperVerdict(BaseModel):
    verdict: Literal["answerable", "unanswerable"]
    question: str = Field(
        default="",
        description="Concise private question for the represented user; empty when verdict is answerable.",
    )


@dataclass(frozen=True)
class GatekeeperRequest:
    instructions: str
    latest_utterance: str
    context: dict
    updates_from_user: tuple[str, ...]

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "developer", "content": self.instructions},
            {
                "role": "user",
                "content": (
                    f"KNOWN CONTEXT:\n{json.dumps(self.context, ensure_ascii=False)}\n\n"
                    f"UPDATES FROM USER THIS CALL:\n{json.dumps(self.updates_from_user, ensure_ascii=False)}\n\n"
                    f"LATEST REPRESENTATIVE UTTERANCE:\n{self.latest_utterance}"
                ),
            },
        ]


def gatekeeper_request(
    latest_utterance: str,
    context: dict,
    updates_from_user: list[str],
) -> GatekeeperRequest:
    return GatekeeperRequest(
        instructions=GATEKEEPER_INSTRUCTIONS,
        latest_utterance=latest_utterance.strip(),
        context=context,
        updates_from_user=tuple(updates_from_user),
    )


class Gatekeeper(Protocol):
    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict: ...


class AllowAllGatekeeper:
    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict:
        return GatekeeperVerdict(verdict="answerable")


class OpenAIGatekeeper:
    def __init__(
        self,
        api_key: Callable[[], str],
        model: Callable[[], str],
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client_factory = client_factory
        self._client = None
        self._client_api_key = ""
        self._client_lock = Lock()

    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict:
        return await asyncio.to_thread(self._classify, request)

    def _classify(self, request: GatekeeperRequest) -> GatekeeperVerdict:
        client = self._client_for_key(self._api_key())
        response = client.responses.parse(
            model=self._model(),
            input=request.messages(),
            reasoning={"effort": "none"},
            text_format=GatekeeperVerdict,
        )
        verdict = response.output_parsed
        if verdict is None:
            raise RuntimeError("Gatekeeper returned no verdict.")
        if verdict.verdict == "unanswerable" and not verdict.question.strip():
            raise RuntimeError("Gatekeeper omitted the user question.")
        return verdict

    def _client_for_key(self, api_key: str):
        with self._client_lock:
            if self._client is not None and self._client_api_key == api_key:
                return self._client
            if self._client_factory is None:
                from openai import OpenAI

                self._client = OpenAI(api_key=api_key)
            else:
                self._client = self._client_factory(api_key=api_key)
            self._client_api_key = api_key
            return self._client
