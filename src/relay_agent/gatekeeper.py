from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

from pydantic import BaseModel, Field


GATEKEEPER_INSTRUCTIONS = (
    "You are Relay's call Gatekeeper. An IDENTITY block will tell you the represented user's name (whose authority "
    "you protect — private questions are written TO this person) and the representative's name (the person/"
    "organization Relay called — this is who said the LATEST UTTERANCE you are reviewing). Never attribute the "
    "representative's own words or statements to the represented user, and never attribute the represented user's "
    "private answers to the representative — these are two different people and mixing them up is a serious error. "
    "Apply this USER AUTHORITY RULE: Relay may continue autonomously only when its response requires no new fact, "
    "preference, judgment, permission, commitment, correction, or consequential choice from the represented user. "
    "Use route=consult_user if responding would require guessing a user-owned fact; choosing between alternatives; "
    "accepting, rejecting, or countering terms; expressing a preference; granting permission; scheduling or "
    "canceling; enrolling, purchasing, or committing; correcting user information; or otherwise exercising authority "
    "on the user's behalf. A known budget, preference, or goal is a constraint, not permission to decide or commit. "
    "When uncertain, use consult_user with reason=uncertainty. Use route=continue with reason=none only for ordinary "
    "conversation that is fully supported by confirmed context and requires no user authority, including greetings, "
    "acknowledgements, procedural questions, and questions about facts the representative owns. A short 'hello?' or "
    "'are you there?' mid-call, after Relay has already been speaking with this representative, is a connectivity "
    "check, not a request to restart the conversation or a question needing the represented user — route=continue, "
    "reply briefly to confirm you're listening and continue from where the conversation left off. INCOMPLETE "
    "FRAGMENTS: if the latest utterance is clearly cut off or trailing (for example 'On', 'Um...', 'Well, you know, "
    "if you want to...') and does not yet contain a complete question, offer, or decision, do NOT consult the user "
    "about a half-sentence — route=continue so Relay can wait or briefly prompt the representative to finish; only "
    "consult once an actual question, offer, or decision has fully landed. CLOSING PLEASANTRIES: once the call's "
    "business is essentially settled, a goodbye, thanks, or wind-down remark from the representative (for example "
    "'okay, I confirm that, thank you, bye-bye' or 'nothing else, bye') is the call ending normally — route=continue "
    "so Relay can acknowledge and close warmly; do NOT consult the user asking whether to keep going or continue. "
    "For consult_user, "
    "faithfully summarize the representative's relevant question, proposal, or new information in "
    "representative_update without changing its meaning or its speaker, then write one direct question_to_user "
    "addressed to the represented user by the IDENTITY block. Never ask the user to supply a fact the representative "
    "just supplied. A related fact is not a substitute for an exact missing fact; for example, a street address does "
    "not answer an apartment-number question. Do not answer the representative."
)

AUTHORITY_VETO_INSTRUCTIONS = (
    "You are Relay's veto-only user-authority checker. You cannot authorize speech; you may only block it. An "
    "IDENTITY block will tell you the represented user's name (whose authority you protect) and the representative's "
    "name (who said the LATEST UTTERANCE). Never attribute the representative's own words to the represented user or "
    "vice versa. Judge ONLY whether responding to the representative's LATEST UTTERANCE, right now, specifically "
    "requires new user authority — not whether the call's eventual purpose might. The call's overall goal (for "
    "example negotiating price, enrolling, or scheduling) is background context, not evidence that THIS utterance "
    "needs the user: a greeting, acknowledgement, procedural remark, a mid-call 'hello?'/'are you there?' "
    "connectivity check, or a question already answered by confirmed context never requires user authority, no "
    "matter what the call may lead to later. Set requires_user=true only "
    "when responding to this specific utterance would itself require guessing a new user-owned fact; choosing "
    "between alternatives; accepting, rejecting, or countering terms; expressing a preference; granting permission; "
    "scheduling or canceling; enrolling, purchasing, or committing; or correcting user information — right now, in "
    "direct response to what the representative just said. When genuinely uncertain whether THIS utterance (not "
    "the call overall) requires authority, veto with reason=uncertainty. Set false whenever continuing clearly "
    "requires no user authority for this utterance specifically. If vetoing, faithfully summarize the "
    "representative's relevant message (correctly attributed to the representative) and ask the represented user "
    "one direct question."
)

UserAuthorityReason = Literal[
    "none",
    "missing_fact",
    "preference",
    "decision",
    "approval",
    "authority",
    "correction",
    "uncertainty",
]


class GatekeeperVerdict(BaseModel):
    route: Literal["continue", "consult_user"]
    reason: UserAuthorityReason
    representative_update: str = Field(
        default="",
        description="Faithful relevant content from the representative when user consultation is required.",
    )
    question_to_user: str = Field(
        default="",
        description="Concise private question for the represented user; empty when route is continue.",
    )


class AuthorityVeto(BaseModel):
    requires_user: bool
    reason: UserAuthorityReason
    representative_update: str = ""
    question_to_user: str = ""


def _validate_authority_route(
    route: str,
    reason: str,
    representative_update: str,
    question_to_user: str,
) -> None:
    if route == "continue":
        if reason != "none":
            raise RuntimeError("An autonomous continuation must use reason=none.")
        return
    if reason == "none":
        raise RuntimeError("User consultation requires a specific authority reason.")
    if not representative_update.strip():
        raise RuntimeError("User consultation omitted the representative's relevant message.")
    if not question_to_user.strip():
        raise RuntimeError("User consultation omitted the private user question.")


class ContextUpdate(BaseModel):
    kind: Literal["fact", "preference", "decision", "call_instruction"]
    key: str = Field(description="Short stable label for the information, such as apartment_number or budget.")
    value: str = Field(description="The represented user's confirmed information, reformulated without commentary.")
    summary: str = Field(description="Concise representative-facing meaning of this update.")


class PrivateMessageRoute(BaseModel):
    disposition: Literal["answer", "context_update", "call_instruction", "private_meta"]
    speaker_update: ContextUpdate | None = None
    private_reply: str = ""


@dataclass(frozen=True)
class GatekeeperRequest:
    instructions: str
    latest_utterance: str
    context: dict
    context_updates: tuple[dict, ...]
    represented_user: str = "the represented user"
    representative_name: str = "the representative"

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "developer", "content": self.instructions},
            {
                "role": "user",
                "content": (
                    f"IDENTITY:\n"
                    f"- Represented user (whose authority you protect; private questions go to them): "
                    f"{self.represented_user}\n"
                    f"- Representative (who said the LATEST UTTERANCE below; Relay called them): "
                    f"{self.representative_name}\n\n"
                    f"KNOWN CONTEXT:\n{json.dumps(self.context, ensure_ascii=False)}\n\n"
                    f"CONFIRMED CONTEXT UPDATES THIS CALL:\n{json.dumps(self.context_updates, ensure_ascii=False)}\n\n"
                    f"LATEST REPRESENTATIVE UTTERANCE (said by {self.representative_name}):\n{self.latest_utterance}"
                ),
            },
        ]


def gatekeeper_request(
    latest_utterance: str,
    context: dict,
    context_updates: list[dict],
    represented_user: str = "the represented user",
    representative_name: str = "the representative",
) -> GatekeeperRequest:
    return GatekeeperRequest(
        instructions=GATEKEEPER_INSTRUCTIONS,
        latest_utterance=latest_utterance.strip(),
        context=context,
        context_updates=tuple(context_updates),
        represented_user=represented_user or "the represented user",
        representative_name=representative_name or "the representative",
    )


@dataclass(frozen=True)
class PrivateMessageRequest:
    text: str
    context: dict
    context_updates: tuple[dict, ...]
    waiting_for_user: bool
    pending_question: str
    pending_reason: str = ""
    pending_interaction_id: str = ""
    represented_user: str = "the represented user"
    representative_name: str = "the representative"

    def messages(self) -> list[dict[str, str]]:
        return [
            {
                "role": "developer",
                "content": (
                    "You are Relay's private call coordinator. The PRIVATE DASHBOARD MESSAGE below comes from the "
                    "represented user (named in the IDENTITY section), not from the representative — never confuse "
                    "the two when writing a Speaker update. Route a dashboard message without ever exposing its "
                    "raw text to the voice Speaker. When WAITING FOR USER is true and PENDING QUESTION is non-empty, "
                    "strongly presume the private dashboard message is the answer to that pending question. Short or "
                    "terse replies such as '7A', 'yes', '$100', or a date are normal answers: use disposition=answer "
                    "even when they are fragments and contain no explanation. Brevity alone is never a reason to use "
                    "private_meta. Override this presumption only when the message clearly cannot answer the pending "
                    "question, such as a question about Relay itself or content obviously unrelated to the pending "
                    "question's subject. Reserve private_meta for genuinely off-topic or meta conversation. Use "
                    "context_update for a confirmed fact, preference, or decision that should inform later turns, and "
                    "call_instruction for a direction Relay should carry out aloud now. Reformulate any Speaker update "
                    "as neutral representative-facing information; never include private chatter or an acknowledgement "
                    "such as 'Got it.' A question addressed to Relay itself, testing message, unrelated aside, or other "
                    "meta conversation is private_meta: answer it briefly in private_reply and provide no Speaker update. "
                    "When disposition is answer, speaker_update is mandatory: copy the factual meaning of the private "
                    "message into its value and summary instead of restating that the answer is missing. "
                    "When PENDING REASON is decision, approval, or authority, use kind=decision so Speaker receives "
                    "an explicit authorization record rather than treating the answer as an ordinary fact. "
                    "Derive a stable, semantically specific key from PENDING QUESTION, such as apartment_number, "
                    "installation_date, or monthly_budget. The summary must state what the answer means, such as "
                    "'The apartment/unit number is 2711.' Never use a generic key such as pending_question_answer, "
                    "user_answer, or answer, and never write a summary that only says the user answered a question."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"IDENTITY:\n"
                    f"- Represented user (the PRIVATE DASHBOARD MESSAGE below is FROM this person): "
                    f"{self.represented_user}\n"
                    f"- Representative (Relay called them; they never send dashboard messages): "
                    f"{self.representative_name}\n\n"
                    f"ORIGINAL CALL CONTEXT:\n{json.dumps(self.context, ensure_ascii=False)}\n\n"
                    f"CONFIRMED CONTEXT UPDATES:\n{json.dumps(self.context_updates, ensure_ascii=False)}\n\n"
                    f"WAITING FOR USER: {self.waiting_for_user}\n"
                    f"PENDING QUESTION: {self.pending_question}\n\n"
                    f"PENDING REASON: {self.pending_reason}\n"
                    f"PENDING INTERACTION ID: {self.pending_interaction_id}\n\n"
                    f"PRIVATE DASHBOARD MESSAGE:\n{self.text}"
                ),
            },
        ]


class Gatekeeper(Protocol):
    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict: ...

    async def veto(self, request: GatekeeperRequest) -> AuthorityVeto: ...

    async def route_private_message(self, request: PrivateMessageRequest) -> PrivateMessageRoute: ...


class AllowAllGatekeeper:
    async def classify(self, request: GatekeeperRequest) -> GatekeeperVerdict:
        return GatekeeperVerdict(route="continue", reason="none")

    async def veto(self, request: GatekeeperRequest) -> AuthorityVeto:
        return AuthorityVeto(requires_user=False, reason="none")

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

    async def veto(self, request: GatekeeperRequest) -> AuthorityVeto:
        return await asyncio.to_thread(self._veto, request)

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
        _validate_authority_route(verdict.route, verdict.reason, verdict.representative_update, verdict.question_to_user)
        return verdict

    def _veto(self, request: GatekeeperRequest) -> AuthorityVeto:
        client = self._client_for_key(self._api_key())
        messages = request.messages()
        messages[0] = {"role": "developer", "content": AUTHORITY_VETO_INSTRUCTIONS}
        response = client.responses.parse(
            model=self._model(),
            input=messages,
            reasoning={"effort": "none"},
            text_format=AuthorityVeto,
        )
        veto = response.output_parsed
        if veto is None:
            raise RuntimeError("Authority checker returned no verdict.")
        route = "consult_user" if veto.requires_user else "continue"
        _validate_authority_route(route, veto.reason, veto.representative_update, veto.question_to_user)
        return veto

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
            route = route.model_copy(update={"disposition": "context_update"})
        if route.disposition == "private_meta":
            return route.model_copy(
                update={
                    "speaker_update": None,
                    "private_reply": route.private_reply.strip() or "I kept that message in our private workspace.",
                }
            )
        if route.disposition == "answer":
            speaker_update = route.speaker_update
            if speaker_update is None or speaker_update.key.strip().lower() in {
                "answer",
                "pending_question_answer",
                "user_answer",
            }:
                speaker_update = _pending_question_update(request.pending_question, request.text)
            else:
                speaker_update = speaker_update.model_copy(update={"value": request.text.strip()})
            if request.pending_reason in {"decision", "approval", "authority"}:
                speaker_update = speaker_update.model_copy(update={"kind": "decision"})
            return route.model_copy(
                update={
                    "speaker_update": speaker_update,
                    "private_reply": "",
                }
            )
        if route.speaker_update is not None:
            return route
        raise RuntimeError("Gatekeeper omitted the required Speaker update.")

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


def _pending_question_update(question: str, answer: str) -> ContextUpdate:
    lowered = question.lower()
    value = answer.strip()
    if "apartment" in lowered or "unit number" in lowered:
        key = "apartment_number"
        summary = f"The apartment/unit number is {value}."
    elif "date of birth" in lowered or "birth date" in lowered or "dob" in lowered:
        key = "date_of_birth"
        summary = f"The date of birth is {value}."
    elif "installation" in lowered and any(word in lowered for word in ("date", "day", "when")):
        key = "installation_date"
        summary = f"The requested installation date or time is {value}."
    elif "budget" in lowered:
        key = "budget"
        summary = f"The represented person's budget is {value}."
    elif "address" in lowered:
        key = "address"
        summary = f"The requested address is {value}."
    elif "email" in lowered:
        key = "email_address"
        summary = f"The email address is {value}."
    else:
        words = re.findall(r"[a-z0-9]+", lowered)
        ignored = {
            "a", "an", "are", "do", "does", "for", "is", "it", "of", "please", "the", "their",
            "they", "to", "user", "what", "when", "which", "who", "your",
        }
        label = [word for word in words if word not in ignored][:5]
        key = "_".join(label) or "confirmed_response"
        summary = f'For the question "{question.strip()}", the confirmed answer is {value}.'
    return ContextUpdate(kind="fact", key=key, value=value, summary=summary)
