from __future__ import annotations

import os
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class PlannerError(RuntimeError):
    pass


class PlanAction(BaseModel):
    kind: Literal["phone_call", "research", "document_review", "other"]
    label: str
    purpose: str
    target: str
    needs_lookup: bool


class PlanningTurn(BaseModel):
    status: Literal["needs_input", "plan_ready"]
    message: str
    plan_summary: str
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

        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5.6")
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
                    "facts, completed research, or completed calls. Mark contact discovery as research when needed. "
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
