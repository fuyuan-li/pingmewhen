from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator


GATEKEEPER_INSTRUCTIONS = (
    "You are Relay's call Gatekeeper. Decide whether the voice Speaker can answer the representative's latest "
    "utterance without private help from the represented user. Return answerable when Speaker can respond faithfully "
    "from the supplied task context and user updates, or when no private fact, preference, decision, or authority is "
    "needed, including greetings, acknowledgements, and questions about facts the representative owns. Return "
    "unanswerable only when responding requires guessing or inventing the user's personal fact, preference, decision, "
    "or authority. A representative's direct request for a personal, account, property, scheduling, or preference "
    "detail is unanswerable unless that exact detail is explicit; a related fact is not a substitute. For example, a "
    "street address does not make an unknown apartment number answerable. For unanswerable, write one concise question "
    "to show privately to the represented user. Do not answer the representative and do not add explanations."
)


class GatekeeperVerdict(BaseModel):
    verdict: Literal["answerable", "unanswerable"]
    question: str = Field(
        default="",
        description="Concise private question for the represented user; empty when verdict is answerable.",
    )


class ContextUpdate(BaseModel):
    kind: Literal["fact", "preference", "decision", "call_instruction"]
    key: str = Field(description="Short stable label for the information, such as apartment_number or budget.")
    value: str = Field(description="The represented user's confirmed information, reformulated without commentary.")
    summary: str = Field(description="Concise representative-facing meaning of this update.")


class PrivateMessageRoute(BaseModel):
    disposition: Literal["answer", "context_update", "call_instruction", "private_meta"]
    speaker_update: ContextUpdate | None = None
    private_reply: str = ""

    @model_validator(mode="after")
    def validate_route(self) -> "PrivateMessageRoute":
        if self.disposition == "private_meta":
            if self.speaker_update is not None or not self.private_reply.strip():
                raise ValueError("Private meta messages require a private reply and no Speaker update.")
        elif self.speaker_update is None:
            raise ValueError("Representative-facing messages require a structured Speaker update.")
        return self


@dataclass(frozen=True)
class GatekeeperRequest:
    instructions: str
    latest_utterance: str
    context: dict
    context_updates: tuple[dict, ...]

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "developer", "content": self.instructions},
            {
                "role": "user",
                "content": (
                    f"KNOWN CONTEXT:\n{json.dumps(self.context, ensure_ascii=False)}\n\n"
                    f"CONFIRMED CONTEXT UPDATES THIS CALL:\n{json.dumps(self.context_updates, ensure_ascii=False)}\n\n"
                    f"LATEST REPRESENTATIVE UTTERANCE:\n{self.latest_utterance}"
                ),
            },
        ]


def gatekeeper_request(
    latest_utterance: str,
    context: dict,
    context_updates: list[dict],
) -> GatekeeperRequest:
    return GatekeeperRequest(
        instructions=GATEKEEPER_INSTRUCTIONS,
        latest_utterance=latest_utterance.strip(),
        context=context,
        context_updates=tuple(context_updates),
    )


@dataclass(frozen=True)
class PrivateMessageRequest:
    text: str
    context: dict
    context_updates: tuple[dict, ...]
    waiting_for_user: bool
    pending_question: str

    def messages(self) -> list[dict[str, str]]:
        return [
            {
                "role": "developer",
                "content": (
                    "You are Relay's private call coordinator. Route a dashboard message without ever exposing its "
                    "raw text to the voice Speaker. If Relay is waiting for an answer, use disposition=answer only "
                    "when the message actually answers the pending question. Otherwise keep it private. Use "
                    "context_update for a confirmed fact, preference, or decision that should inform later turns, and "
                    "call_instruction for a direction Relay should carry out aloud now. Reformulate any Speaker update "
                    "as neutral representative-facing information; never include private chatter or an acknowledgement "
                    "such as 'Got it.' A question addressed to Relay itself, testing message, unrelated aside, or other "
                    "meta conversation is private_meta: answer it briefly in private_reply and provide no Speaker update."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"ORIGINAL CALL CONTEXT:\n{json.dumps(self.context, ensure_ascii=False)}\n\n"
                    f"CONFIRMED CONTEXT UPDATES:\n{json.dumps(self.context_updates, ensure_ascii=False)}\n\n"
                    f"WAITING FOR USER: {self.waiting_for_user}\n"
                    f"PENDING QUESTION: {self.pending_question}\n\n"
                    f"PRIVATE DASHBOARD MESSAGE:\n{self.text}"
                ),
            },
        ]


class Gatekeeper(Protocol):
    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict: ...

    async def route_private_message(self, request: PrivateMessageRequest) -> PrivateMessageRoute: ...


class AllowAllGatekeeper:
    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict:
        return GatekeeperVerdict(verdict="answerable")

    async def route_private_message(self, request: PrivateMessageRequest) -> PrivateMessageRoute:
        disposition = "answer" if request.waiting_for_user else "call_instruction"
        return PrivateMessageRoute(
            disposition=disposition,
            speaker_update=ContextUpdate(
                kind="fact" if request.waiting_for_user else "call_instruction",
                key="user_answer" if request.waiting_for_user else "call_instruction",
                value=request.text.strip(),
                summary=request.text.strip(),
            ),
        )


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

    async def route_private_message(self, request: PrivateMessageRequest) -> PrivateMessageRoute:
        return await asyncio.to_thread(self._route_private_message, request)

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

    def _route_private_message(self, request: PrivateMessageRequest) -> PrivateMessageRoute:
        client = self._client_for_key(self._api_key())
        response = client.responses.parse(
            model=self._model(),
            input=request.messages(),
            reasoning={"effort": "none"},
            text_format=PrivateMessageRoute,
        )
        route = response.output_parsed
        if route is None:
            raise RuntimeError("Gatekeeper returned no private-message route.")
        if route.disposition == "answer" and not request.waiting_for_user:
            raise RuntimeError("Gatekeeper marked a message as an answer when no answer was pending.")
        return route

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
