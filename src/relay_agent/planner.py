from __future__ import annotations

import os
import re
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator


class PlannerError(RuntimeError):
    pass


class PlanAction(BaseModel):
    kind: Literal["phone_call", "research", "document_review", "other"]
    label: str
    purpose: str = Field(
        description=(
            "Clean third-person natural-language description of the call's purpose and desired outcome, with concrete "
            "facts such as full addresses, dates, constraints, and names written literally. Never address Relay with "
            "an imperative instruction, never include the phone number or routing metadata, and never use vague "
            "placeholders such as 'the provided address'."
        )
    )
    target: str
    known_facts: list[str] = Field(
        default_factory=list,
        description="Concrete facts already established for this action, copied literally rather than paraphrased.",
    )
    needs_lookup: bool
    phone_number: str = Field(description="Exact E.164 number for executable phone calls, otherwise an empty string.")
    contact_provided_by: Literal["user", "research"] = Field(
        description="Whether the phone number came directly from the user or from Relay's research."
    )
    contact_source_url: str = Field(
        description="Official source URL for a researched phone number; empty for a user-provided number."
    )

    @model_validator(mode="after")
    def reject_vague_phone_facts(self) -> "PlanAction":
        if self.kind != "phone_call":
            return self
        vague_fact = re.search(
            r"\b(?:"
            r"provided(?:\s+\w+){0,3}\s+(?:address|date|details?|information)|"
            r"(?:address|date|details?|information)\s+provided|"
            r"(?:above|aforementioned)\s+(?:address|date|details?|information)|"
            r"the\s+user'?s\s+(?:details?|information)"
            r")\b",
            self.purpose,
            re.IGNORECASE,
        )
        if vague_fact:
            raise ValueError("Phone-call purpose must inline concrete known facts instead of vague placeholders.")
        if re.search(r"\+[1-9](?:[\s().-]*\d){7,14}", self.purpose):
            raise ValueError("Phone-call purpose must not duplicate the structured phone number.")
        if re.match(
            r"^\s*(?:please\s+)?(?:call|dial|contact)\b|^\s*请(?:致电|拨打|联系|帮忙)",
            self.purpose,
            re.IGNORECASE,
        ):
            raise ValueError("Phone-call purpose must describe the goal rather than instruct Relay to place the call.")
        return self


class PlanningTurn(BaseModel):
    status: Literal["needs_input", "plan_ready"]
    message: str
    plan_summary: str
    caller_name: str = Field(
        default="",
        description="Confirmed display name or nickname to use when calling on the user's behalf; empty if unknown.",
    )
    actions: list[PlanAction] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Planner(Protocol):
    ready: bool
    model: str

    def plan(self, goal: str, messages: list[dict[str, str]], contexts: list[dict[str, str]]) -> PlanningTurn: ...


class UnavailablePlanner:
    ready = False
    model = "unavailable"

    def plan(self, goal: str, messages: list[dict[str, str]], contexts: list[dict[str, str]]) -> PlanningTurn:
        raise PlannerError(
            "Relay's production planner needs your OpenAI API key. "
            "Complete local setup in the dashboard or set OPENAI_API_KEY."
        )


class OpenAIPlanner:
    ready = True

    def __init__(self, api_key: str, model: str | None = None) -> None:
        from openai import OpenAI

        self.model = model or "gpt-5.4-mini"
        self._client = OpenAI(api_key=api_key)

    def plan(self, goal: str, messages: list[dict[str, str]], contexts: list[dict[str, str]]) -> PlanningTurn:
        context_block = "\n\n".join(
            f"DOCUMENT: {item['filename']}\n{item['text']}" for item in contexts if item.get("text")
        ) or "No document text was provided."
        input_messages = [
            {
                "role": "developer",
                "content": (
                    "You are Relay's private task planner. Turn the user's goal and documents into a concrete, "
                    "reviewable plan. Ask only for genuinely blocking information. Never invent contact details, "
                    "facts, completed research, or completed calls. Use web search when current contact details are "
                    "needed, prefer the organization's official website, set contact_provided_by to research, and "
                    "record the official source URL. When the user directly supplies a personal or business phone "
                    "number, set contact_provided_by to user and leave contact_source_url empty; do not invent or seek "
                    "a URL merely to justify a number the user provided. Set needs_lookup to false only when the number "
                    "is ready to call. A phone_call is executable only when phone_number is exact E.164; otherwise ask "
                    "for honest clarification or make contact discovery an explicit research step. Read the entire "
                    "conversation when interpreting phone numbers and combine fragments supplied across turns. A "
                    "10-digit US or Canadian national number is complete and needs a +1 country prefix; never claim it "
                    "is missing a digit. If the user later supplies '+1', combine it with the prior 10-digit number. If "
                    "country context still needs confirmation, ask explicitly, for example: 'I'll treat this as "
                    "+12027010927 — confirm?' Do not state a specific technical defect in a phone number unless it is "
                    "actually true. "
                    "Create one phone_call action per organization and per separate phone conversation. "
                    "For every phone_call, inline all concrete facts already known from the goal, documents, and "
                    "conversation directly into purpose and known_facts. Preserve exact full addresses, dates, names, "
                    "amounts, constraints, and requested outcomes. Never replace a known value with vague wording such "
                    "as 'the provided address', 'the address above', 'the requested date', or 'the user's details'. "
                    "Write purpose as a clean third-person natural-language description of what the call is for and "
                    "what outcome is desired, never as an imperative instruction addressed to Relay. Do not put a "
                    "phone number, source URL, or other routing metadata in purpose; those belong only in their typed "
                    "fields. A phone number may remain in known_facts as internal reference data. "
                    "The call-time models must be able to execute from the action without rediscovering known facts. "
                    "Before returning plan_ready with any phone_call action, make sure you know the display name or "
                    "nickname Relay should use when introducing the person it represents. If it is not already clear "
                    "from the full conversation, return needs_input and ask once, warmly and in the user's language. "
                    "Briefly explain that Relay needs it to introduce the call on their behalf; a first name or "
                    "nickname is enough, and never demand a legal name. Put the confirmed value in caller_name. If "
                    "the conversation says the caller name is already confirmed, preserve it and do not ask again. "
                    "Phone calls are actions, not the product boundary. Consequential actions always require user "
                    "approval. For regulated choices, organize factual options but do not choose for the user. "
                    "Return plan_ready only when the next actions are specific enough for user approval."
                ),
            },
            {"role": "user", "content": f"GOAL:\n{goal}\n\nLOCAL DOCUMENT EXCERPTS:\n{context_block}"},
            *messages,
        ]
        try:
            response = self._client.responses.parse(
                model=self.model,
                input=input_messages,
                tools=[{"type": "web_search"}],
                text_format=PlanningTurn,
            )
        except Exception as error:
            raise PlannerError(f"The production planner request failed: {error}") from error

        parsed = response.output_parsed
        if parsed is None:
            raise PlannerError("The production planner returned no usable plan.")
        return parsed


def planner_from_environment() -> Planner:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return UnavailablePlanner()
    return OpenAIPlanner(api_key)
