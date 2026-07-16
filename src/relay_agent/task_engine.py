from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from relay_agent.event_log import EventLog


QUOTES = [
    {
        "id": "harbor",
        "insurer": "Harbor Mutual",
        "monthly_premium": "$18.40",
        "personal_property": "$20,000",
        "liability": "$100,000",
        "deductible": "$500",
        "loss_of_use": "$6,000",
    },
    {
        "id": "northstar",
        "insurer": "Northstar Insurance",
        "monthly_premium": "$21.85",
        "personal_property": "$25,000",
        "liability": "$100,000",
        "deductible": "$500",
        "loss_of_use": "$7,500",
    },
    {
        "id": "cedar",
        "insurer": "Cedar Shield",
        "monthly_premium": "$19.75",
        "personal_property": "$20,000",
        "liability": "$300,000",
        "deductible": "$500",
        "loss_of_use": "$6,000",
    },
]


class InvalidAction(ValueError):
    pass


class TaskNotFound(KeyError):
    pass


class DeterministicTaskEngine:
    """A narrow, deterministic workflow used to validate Relay's product experience."""

    def __init__(self, events: EventLog) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._events = events
        self._lock = Lock()

    def create(self, goal: str, contexts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        cleaned_goal = goal.strip()
        if not cleaned_goal:
            raise InvalidAction("Describe what Relay should accomplish.")

        task_id = uuid4().hex
        task: dict[str, Any] = {
            "id": task_id,
            "goal": cleaned_goal,
            "contexts": deepcopy(contexts or []),
            "status": "waiting_for_user",
            "stage": "claims_history",
            "created_at": datetime.now(UTC).isoformat(),
            "events": [],
            "quotes": [],
            "selected_insurer": None,
            "prompt": {
                "kind": "quick_reply",
                "question": "Have you filed any renters or homeowners insurance claims in the last five years?",
                "options": [
                    {"value": "no", "label": "No"},
                    {"value": "yes", "label": "Yes"},
                    {"value": "unsure", "label": "Not sure"},
                ],
            },
            "secure_mode": False,
        }
        self._append(task, "notice", text="Deterministic demo: all insurers, quotes, and payment details are simulated.")
        if task["contexts"]:
            names = ", ".join(context.get("filename", "PDF context") for context in task["contexts"])
            self._append(task, "status", text=f"Local PDF context attached · {names}")
        self._append(task, "status", text="Relay reviewed the goal and prepared a three-call quote plan.")
        self._append(task, "message", speaker="relay", text="I’ll call three insurers, collect factual quote details, and bring the results back without choosing for you.")
        self._append(task, "status", text="Calling simulated insurer 1 of 3 · Harbor Mutual")
        self._append(task, "message", speaker="representative", company="Harbor Mutual", text="Harbor Mutual, this is Maya. How can I help?")
        self._append(
            task,
            "message",
            speaker="relay",
            text=(
                "Hi, I’m Relay, an AI voice assistant speaking for Alex, who is online with us but not convenient "
                "to speak. Alex will provide personal information and decisions by text. Is that okay?"
            ),
        )
        self._append(task, "message", speaker="representative", company="Harbor Mutual", text="That’s fine. I can help with a renters quote.")
        self._append(task, "message", speaker="relay", text="We’d like a quote for the sample apartment and coverage profile provided by Alex.")
        self._append(
            task,
            "message",
            speaker="representative",
            company="Harbor Mutual",
            text="Before I calculate it, has Alex filed any renters or homeowners claims in the last five years?",
        )
        self._append(task, "message", speaker="relay", text="Can you give me just one moment?")

        with self._lock:
            self._tasks[task_id] = task
        self._events.append("task.created", {"task_id": task_id, "goal": cleaned_goal, "stage": task["stage"]})
        return deepcopy(task)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            return deepcopy(task)

    def act(self, task_id: str, action: str, value: str = "") -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)

            if action == "instruction":
                self._instruct(task, value)
            elif action == "takeover":
                self._takeover(task)
            elif action == "answer":
                self._answer(task, value)
            else:
                raise InvalidAction("Unsupported task action.")

            snapshot = deepcopy(task)

        self._events.append(
            "task.action",
            {"task_id": task_id, "action": action, "value": value, "stage": snapshot["stage"]},
        )
        return snapshot

    def _instruct(self, task: dict[str, Any], value: str) -> None:
        instruction = value.strip()
        if not instruction:
            raise InvalidAction("Type an instruction before sending it.")
        if task["status"] == "complete":
            raise InvalidAction("This task is already complete.")
        self._append(task, "message", speaker="user_private", text=instruction)
        self._append(task, "message", speaker="relay_private", text="Understood. I’ll apply that instruction to the active task.")

    def _takeover(self, task: dict[str, Any]) -> None:
        if task["status"] == "complete":
            raise InvalidAction("This task is already complete.")
        if task["stage"] == "takeover":
            raise InvalidAction("You already have the call.")
        task["resume_stage"] = task["stage"]
        task["resume_prompt"] = deepcopy(task["prompt"])
        self._append(task, "message", speaker="user_private", text="Take over the active call now.")
        self._append(task, "message", speaker="relay", text="Can you give me just one moment? Alex is taking over the call now.")
        self._append(task, "status", text="User takeover active · Relay microphone paused")
        task["stage"] = "takeover"
        task["prompt"] = {
            "kind": "approval",
            "question": "You have the simulated call. Return control to Relay when ready.",
            "options": [{"value": "resume", "label": "Return control to Relay"}],
        }

    def _answer(self, task: dict[str, Any], value: str) -> None:
        stage = task["stage"]
        handlers = {
            "claims_history": self._handle_claims,
            "select_insurer": self._handle_selection,
            "approve_callback": self._handle_callback_approval,
            "confirm_application": self._handle_application_confirmation,
            "payment_method": self._handle_payment_method,
            "risky_confirmation": self._handle_risky_confirmation,
            "secure_complete": self._handle_secure_complete,
            "takeover": self._handle_resume,
        }
        handler = handlers.get(stage)
        if handler is None:
            raise InvalidAction("Relay is not waiting for an answer right now.")
        handler(task, value)

    def _handle_resume(self, task: dict[str, Any], value: str) -> None:
        if value != "resume":
            raise InvalidAction("Return control to Relay to continue.")
        self._append(task, "message", speaker="user_private", text="Relay, you can continue.")
        self._append(task, "status", text="Relay resumed the simulated call")
        task["stage"] = task.pop("resume_stage")
        task["prompt"] = task.pop("resume_prompt")

    def _handle_claims(self, task: dict[str, Any], value: str) -> None:
        answers = {"no": "No", "yes": "Yes", "unsure": "Not sure"}
        if value not in answers:
            raise InvalidAction("Choose one of the provided claim-history answers.")
        answer = answers[value]
        self._append(task, "message", speaker="user_private", text=answer)
        self._append(task, "message", speaker="relay", text=f"Thanks for waiting. Alex’s answer is: {answer.lower()}.")
        self._append(task, "message", speaker="representative", company="Harbor Mutual", text="Great. The simulated quote is $18.40 per month with a $500 deductible.")
        self._append(task, "status", text="Harbor Mutual call completed · quote captured")
        self._append(task, "status", text="Calling simulated insurer 2 of 3 · Northstar Insurance")
        self._append(task, "message", speaker="representative", company="Northstar Insurance", text="Based on the sample profile, the quote is $21.85 per month. Personal property coverage is $25,000.")
        self._append(task, "message", speaker="relay", text="Thank you. I’ve recorded the premium, limits, deductible, and loss-of-use coverage.")
        self._append(task, "status", text="Northstar Insurance call completed · quote captured")
        self._append(task, "status", text="Calling simulated insurer 3 of 3 · Cedar Shield")
        self._append(task, "message", speaker="representative", company="Cedar Shield", text="The simulated premium is $19.75 per month, including $300,000 in liability coverage.")
        self._append(task, "message", speaker="relay", text="Thank you. I have the factual quote details I need.")
        self._append(task, "status", text="Cedar Shield call completed · quote captured")
        task["quotes"] = deepcopy(QUOTES)
        self._append(task, "comparison", text="Three simulated quotes collected. Relay has not ranked or recommended them.")
        task["stage"] = "select_insurer"
        task["prompt"] = {
            "kind": "insurer_selection",
            "question": "Review the factual comparison and choose which insurer Relay should call back.",
            "options": [{"value": quote["id"], "label": quote["insurer"]} for quote in QUOTES],
        }

    def _handle_selection(self, task: dict[str, Any], value: str) -> None:
        selected = next((quote for quote in QUOTES if quote["id"] == value), None)
        if selected is None:
            raise InvalidAction("Choose one of the quoted insurers.")
        task["selected_insurer"] = selected["insurer"]
        self._append(task, "message", speaker="user_private", text=f"Continue with {selected['insurer']}.")
        self._append(
            task,
            "message",
            speaker="relay_private",
            text=f"You selected {selected['insurer']}. I need your approval before placing the application callback.",
        )
        task["stage"] = "approve_callback"
        task["prompt"] = {
            "kind": "approval",
            "question": f"Approve a simulated callback to {selected['insurer']} to continue the application?",
            "options": [
                {"value": "approve", "label": "Approve callback"},
                {"value": "stop", "label": "Stop here"},
            ],
        }

    def _handle_callback_approval(self, task: dict[str, Any], value: str) -> None:
        if value == "stop":
            self._append(task, "message", speaker="user_private", text="Stop here. Do not call back.")
            self._append(task, "status", text="Task stopped before application callback. No purchase was made.")
            task["stage"] = "complete"
            task["status"] = "complete"
            task["prompt"] = None
            return
        if value != "approve":
            raise InvalidAction("Approve the callback or stop the task.")
        insurer = task["selected_insurer"]
        self._append(task, "message", speaker="user_private", text="Approved. Place the callback.")
        self._append(task, "status", text=f"Calling {insurer} · continuing simulated application")
        self._append(task, "message", speaker="representative", company=insurer, text="Welcome back. I found the quote. Are you ready to continue with the application?")
        self._append(task, "message", speaker="relay", text="Yes. Alex selected this quote and approved this callback. Alex will personally confirm the application details.")
        task["stage"] = "confirm_application"
        task["prompt"] = {
            "kind": "approval",
            "question": "The simulated representative asks you to confirm that the sample application details are accurate.",
            "options": [
                {"value": "confirm", "label": "I confirm"},
                {"value": "takeover", "label": "Take over"},
            ],
        }

    def _handle_application_confirmation(self, task: dict[str, Any], value: str) -> None:
        if value not in {"confirm", "takeover"}:
            raise InvalidAction("Confirm the sample details or take over.")
        if value == "takeover":
            self._append(task, "message", speaker="user_private", text="I’ll take over from here.")
            self._append(task, "message", speaker="relay", text="Can you give me just one moment? Alex is taking over the call now.")
            self._append(task, "status", text="User takeover active · Relay microphone and transcription paused")
        else:
            self._append(task, "message", speaker="user_private", text="I confirm the sample application details are accurate.")
            self._append(task, "message", speaker="relay", text="Alex confirms the sample application details are accurate.")
            self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="Thank you. The final step is the simulated payment.")
        task["stage"] = "payment_method"
        task["prompt"] = {
            "kind": "payment_method",
            "question": "Choose how to handle the simulated payment segment. Use fake card data only.",
            "options": [
                {"value": "takeover", "label": "Take over (recommended)"},
                {"value": "local_tts", "label": "Use local device voice"},
                {"value": "risky", "label": "Let Relay speak it · risky"},
            ],
        }

    def _handle_payment_method(self, task: dict[str, Any], value: str) -> None:
        if value == "risky":
            self._append(task, "notice", text="Risk warning: sending payment data through the agent would expose it to the cloud model context.")
            task["stage"] = "risky_confirmation"
            task["prompt"] = {
                "kind": "approval",
                "question": "This option is not recommended. Continue only with fake test data?",
                "options": [
                    {"value": "continue", "label": "Continue with fake data"},
                    {"value": "cancel", "label": "Choose safer option"},
                ],
            }
            return
        if value not in {"takeover", "local_tts"}:
            raise InvalidAction("Choose one of the payment handling methods.")
        self._enter_secure_mode(task, value)

    def _handle_risky_confirmation(self, task: dict[str, Any], value: str) -> None:
        if value == "cancel":
            task["stage"] = "payment_method"
            task["prompt"] = {
                "kind": "payment_method",
                "question": "Choose a safer simulated payment method.",
                "options": [
                    {"value": "takeover", "label": "Take over (recommended)"},
                    {"value": "local_tts", "label": "Use local device voice"},
                    {"value": "risky", "label": "Let Relay speak it · risky"},
                ],
            }
            return
        if value != "continue":
            raise InvalidAction("Continue with fake data or choose a safer option.")
        self._enter_secure_mode(task, "risky")

    def _enter_secure_mode(self, task: dict[str, Any], method: str) -> None:
        task["secure_mode"] = True
        labels = {
            "takeover": "User takeover",
            "local_tts": "Local device voice",
            "risky": "Relay voice simulation",
        }
        self._append(task, "message", speaker="relay", text="One moment—Alex will handle the payment details through a protected channel.")
        self._append(task, "status", text=f"Secure mode active · {labels[method]} · cloud transcription paused")
        self._append(task, "secure_gap", text="Sensitive payment segment is neither transcribed nor logged.")
        task["stage"] = "secure_complete"
        task["prompt"] = {
            "kind": "secure_entry" if method == "local_tts" else "secure_complete",
            "method": method,
            "question": (
                "Enter fake test card data locally, speak it with the device voice, then complete the simulated segment."
                if method == "local_tts"
                else "Complete the simulated protected payment segment when ready."
            ),
            "options": [{"value": "complete", "label": "Complete simulated payment"}],
        }

    def _handle_secure_complete(self, task: dict[str, Any], value: str) -> None:
        if value != "complete":
            raise InvalidAction("Complete the simulated secure segment to continue.")
        task["secure_mode"] = False
        self._append(task, "status", text="Secure mode ended · Relay reconnected · transcription resumed")
        self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="The sandbox payment was accepted. The sample policy is active immediately.")
        self._append(task, "message", speaker="relay", text=f"Done. The simulated policy with {task['selected_insurer']} is active. I saved the non-sensitive outcome and full visible transcript.")
        self._append(task, "status", text="Task complete · simulated confirmation captured")
        task["stage"] = "complete"
        task["status"] = "complete"
        task["prompt"] = None

    def _append(self, task: dict[str, Any], event_type: str, **payload: Any) -> None:
        event = {
            "id": len(task["events"]) + 1,
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        task["events"].append(event)
        if event_type == "message":
            self._events.append(
                "transcript.turn",
                {
                    "task_id": task["id"],
                    "speaker": payload.get("speaker"),
                    "company": payload.get("company"),
                    "text": payload.get("text"),
                },
            )
        else:
            self._events.append(
                f"workflow.{event_type}",
                {"task_id": task["id"], **payload},
            )
