from __future__ import annotations

import re
from typing import Any


def user_input_prompt(
    question: str,
    *,
    blocking: bool = True,
    response_action: str = "instruction",
) -> dict[str, Any]:
    cleaned = question.strip()
    question_line = next((line.strip() for line in reversed(cleaned.splitlines()) if line.strip()), cleaned)
    lowered = question_line.lower()
    prompt: dict[str, Any] = {
        "kind": "text_reply",
        "question": cleaned,
        "options": [],
        "blocking": blocking,
        "response_action": response_action,
        "input_kind": "text",
        "placeholder": "Type your answer…",
    }
    if re.search(r"\b(date of birth|birth date|birthdate|dob)\b", lowered):
        prompt.update(
            kind="date_input",
            input_kind="date",
            placeholder="Choose a date",
            field="date_of_birth",
        )
        return prompt
    if re.search(r"\b(installation|appointment|delivery|service|move[- ]?in)\b", lowered) and re.search(
        r"\b(date|day|when)\b", lowered
    ):
        prompt.update(kind="date_input", input_kind="date", placeholder="Choose a date")
        return prompt
    if re.search(r"\b(account|confirmation|reference|member|policy)\s+(?:number|no\.?|#|id)\b", lowered):
        prompt.update(
            kind="masked_input",
            input_kind="masked",
            placeholder="Enter the requested identifier",
            hint="Enter only the identifier the representative requested.",
        )
        return prompt
    yes_no_lead = re.match(
        r"^(?:is|are|am|was|were|do|does|did|can|could|will|would|should|have|has|had|may)\b",
        lowered,
    )
    explicit_yes_no = "yes or no" in lowered or "yes/no" in lowered
    if (yes_no_lead or explicit_yes_no) and not re.search(r"\b(?:or|versus|vs\.?)\b", lowered.replace("yes or no", "")):
        prompt.update(
            kind="quick_reply",
            options=[{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
            allow_text=True,
            placeholder="Or type another answer…",
        )
    return prompt
