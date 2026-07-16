from __future__ import annotations

import re
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
        "phone": "(555) 010-0142",
        "monthly_premium": "$18.40",
        "personal_property": "$20,000",
        "liability": "$100,000",
        "deductible": "$500",
        "loss_of_use": "$6,000",
    },
    {
        "id": "northstar",
        "insurer": "Northstar Insurance",
        "phone": "(555) 010-0188",
        "monthly_premium": "$21.85",
        "personal_property": "$25,000",
        "liability": "$100,000",
        "deductible": "$500",
        "loss_of_use": "$7,500",
    },
    {
        "id": "cedar",
        "insurer": "Cedar Shield",
        "phone": "(555) 010-0127",
        "monthly_premium": "$19.75",
        "personal_property": "$20,000",
        "liability": "$300,000",
        "deductible": "$500",
        "loss_of_use": "$6,000",
    },
    {
        "id": "summit",
        "insurer": "Summit Casualty",
        "phone": "(555) 010-0164",
        "monthly_premium": "$20.60",
        "personal_property": "$30,000",
        "liability": "$100,000",
        "deductible": "$1,000",
        "loss_of_use": "$9,000",
    },
]


class InvalidAction(ValueError):
    pass


class TaskNotFound(KeyError):
    pass


def queued(event_type: str, **payload: Any) -> dict[str, Any]:
    return {"type": event_type, **payload}


class DeterministicTaskEngine:
    """An interruptible deterministic workflow for validating Relay's UX."""

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
            "phase": "planning",
            "status": "waiting_for_user",
            "stage": "plan_address",
            "created_at": datetime.now(UTC).isoformat(),
            "events": [],
            "quotes": [],
            "carrier_ids": ["harbor", "northstar", "cedar"],
            "strategy_questions": [],
            "address": None,
            "selected_insurer": None,
            "prompt": {
                "kind": "text_reply",
                "question": "What is the address of the apartment you want to insure? Reply in the private message box below.",
                "options": [],
            },
            "secure_mode": False,
            "auto_advance": False,
            "_pending": [],
            "_pending_prompt": None,
            "_pending_stage": None,
        }
        self._append(task, "notice", text="Deterministic demo: all companies, phone numbers, quotes, and payment details are simulated.")
        self._append(task, "message", speaker="user_private", text=cleaned_goal)
        if task["contexts"]:
            names = ", ".join(context.get("filename", "PDF context") for context in task["contexts"])
            self._append(task, "status", text=f"Local PDF context attached · {names}")
            self._append(
                task,
                "message",
                speaker="relay_private",
                text=(
                    "I attached the PDF to this task. Before I finalize the quote plan, please confirm the rented "
                    "property address so I do not rely on an ambiguous document value."
                ),
            )
        else:
            self._append(
                task,
                "message",
                speaker="relay_private",
                text=(
                    "I can help. Insurers will need the rented property address, and I do not see it in the "
                    "context you provided. What address should I use?"
                ),
            )

        with self._lock:
            self._tasks[task_id] = task
        self._events.append("task.created", {"task_id": task_id, "goal": cleaned_goal, "phase": "planning"})
        return self._snapshot(task)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            return self._snapshot(task)

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
            elif action == "advance":
                self._advance(task)
            else:
                raise InvalidAction("Unsupported task action.")
            snapshot = self._snapshot(task)

        if action != "advance":
            self._events.append(
                "task.action",
                {"task_id": task_id, "action": action, "value": value, "stage": snapshot["stage"]},
            )
        return snapshot

    def _snapshot(self, task: dict[str, Any]) -> dict[str, Any]:
        return deepcopy({key: value for key, value in task.items() if not key.startswith("_")})

    def _instruct(self, task: dict[str, Any], value: str) -> None:
        instruction = value.strip()
        if not instruction:
            raise InvalidAction("Type a message before sending it.")
        if task["status"] == "complete":
            raise InvalidAction("This task is already complete.")

        if task["phase"] == "planning":
            self._planning_message(task, instruction)
        else:
            self._call_interruption(task, instruction)

    def _planning_message(self, task: dict[str, Any], instruction: str) -> None:
        self._append(task, "message", speaker="user_private", text=instruction)
        if task["stage"] == "plan_address":
            task["address"] = instruction
            self._append(
                task,
                "message",
                speaker="relay_private",
                text="Thanks. I’ll use that address for this simulated quote task. Here is the proposed call plan.",
            )
            self._present_plan(task)
            return

        if task["stage"] != "plan_review":
            raise InvalidAction("Relay is not accepting planning changes right now.")

        changed_carriers = self._apply_carrier_edits(task, instruction)
        lower = instruction.lower()
        if "multi" in lower and "discount" in lower:
            question = "Ask whether a multi-policy discount is available."
            if question not in task["strategy_questions"]:
                task["strategy_questions"].append(question)
        elif not changed_carriers:
            task["strategy_questions"].append(instruction)

        self._append(
            task,
            "message",
            speaker="relay_private",
            text="I updated the call brief. Please review the revised plan before approving it.",
        )
        self._present_plan(task)

    def _apply_carrier_edits(self, task: dict[str, Any], instruction: str) -> bool:
        changed = False
        clauses = [clause.strip() for clause in re.split(r"[,;]|\band\b", instruction.lower()) if clause.strip()]
        for clause in clauses:
            for quote in QUOTES:
                names = {quote["id"], quote["insurer"].lower(), quote["insurer"].lower().split()[0]}
                if not any(name in clause for name in names):
                    continue
                if any(marker in clause for marker in ("don't", "do not", "remove", "skip", "exclude")):
                    if quote["id"] in task["carrier_ids"] and len(task["carrier_ids"]) > 1:
                        task["carrier_ids"].remove(quote["id"])
                        changed = True
                elif any(marker in clause for marker in ("add", "include", "call")):
                    if quote["id"] not in task["carrier_ids"]:
                        task["carrier_ids"].append(quote["id"])
                        changed = True
        return changed

    def _present_plan(self, task: dict[str, Any]) -> None:
        carriers = self._selected_quotes(task)
        self._append(
            task,
            "plan",
            text="Proposed call plan",
            address=task["address"],
            carriers=[{"id": quote["id"], "insurer": quote["insurer"], "phone": quote["phone"]} for quote in carriers],
            questions=deepcopy(task["strategy_questions"]),
        )
        task["stage"] = "plan_review"
        task["status"] = "waiting_for_user"
        task["prompt"] = {
            "kind": "approval",
            "question": "Relay is ready to begin the simulated calls. Approve this plan, hold to make edits, or decline.",
            "options": [
                {"value": "approve", "label": "Approve and start calls"},
                {"value": "hold", "label": "Hold · edit plan"},
                {"value": "decline", "label": "Decline"},
            ],
        }

    def _answer(self, task: dict[str, Any], value: str) -> None:
        handlers = {
            "plan_review": self._handle_plan_decision,
            "claims_history": self._handle_claims,
            "select_insurer": self._handle_selection,
            "approve_callback": self._handle_callback_approval,
            "confirm_application": self._handle_application_confirmation,
            "payment_method": self._handle_payment_method,
            "risky_confirmation": self._handle_risky_confirmation,
            "secure_complete": self._handle_secure_complete,
            "takeover": self._handle_resume,
        }
        handler = handlers.get(task["stage"])
        if handler is None:
            raise InvalidAction("Relay is not waiting for that answer right now.")
        handler(task, value)

    def _handle_plan_decision(self, task: dict[str, Any], value: str) -> None:
        if value == "hold":
            self._append(task, "message", speaker="user_private", text="Hold. I want to revise the plan.")
            self._append(task, "message", speaker="relay_private", text="Of course. Tell me what to add, remove, or change.")
            task["prompt"] = {
                "kind": "text_reply",
                "question": "Type your plan changes below. Relay will return a revised plan for approval.",
                "options": [],
            }
            return
        if value == "decline":
            self._append(task, "message", speaker="user_private", text="Decline. Do not start the calls.")
            self._append(task, "status", text="Plan declined · no calls started")
            task.update(phase="complete", stage="complete", status="complete", prompt=None)
            return
        if value != "approve":
            raise InvalidAction("Approve, hold, or decline the call plan.")

        self._append(task, "message", speaker="user_private", text="Approved. Start the calls.")
        self._append(task, "message", speaker="relay_private", text="Approved. I’m switching to the live call monitor now.")
        task["phase"] = "calling"
        task["status"] = "running"
        self._start_first_call(task)

    def _start_first_call(self, task: dict[str, Any]) -> None:
        first = self._selected_quotes(task)[0]
        call_count = len(task["carrier_ids"])
        events = [
            queued("status", text=f"Calling simulated insurer 1 of {call_count} · {first['insurer']}", company=first["insurer"]),
            queued("message", speaker="representative", company=first["insurer"], text=f"{first['insurer']}, this is Maya. How can I help?"),
            queued(
                "message",
                speaker="relay",
                text=(
                    "Hi, I’m Relay, an AI voice assistant speaking for Alex, who is online with us but not convenient "
                    "to speak. Alex will provide personal information and decisions by text. Is that okay?"
                ),
            ),
            queued("message", speaker="representative", company=first["insurer"], text="That’s fine. I can help with a renters quote."),
            queued("message", speaker="relay", text=f"We’d like a quote for {task['address']} using Alex’s sample coverage profile."),
            queued(
                "message",
                speaker="representative",
                company=first["insurer"],
                text="Before I calculate it, has Alex filed any renters or homeowners claims in the last five years?",
            ),
            queued("message", speaker="relay", text="Can you give me just one moment?"),
        ]
        prompt = {
            "kind": "quick_reply",
            "question": "Have you filed any renters or homeowners insurance claims in the last five years?",
            "options": [
                {"value": "no", "label": "No"},
                {"value": "yes", "label": "Yes"},
                {"value": "unsure", "label": "Not sure"},
            ],
        }
        self._schedule(task, events, prompt, "claims_history")

    def _handle_claims(self, task: dict[str, Any], value: str) -> None:
        answers = {"no": "No", "yes": "Yes", "unsure": "Not sure"}
        if value not in answers:
            raise InvalidAction("Choose one of the provided claim-history answers.")
        answer = answers[value]
        self._append(task, "message", speaker="user_private", text=answer)
        carriers = self._selected_quotes(task)
        first = carriers[0]
        events = [
            queued("message", speaker="relay", text=f"Thanks for waiting. Alex’s answer is: {answer.lower()}."),
            queued(
                "message",
                speaker="representative",
                company=first["insurer"],
                text=f"Great. The simulated quote is {first['monthly_premium']} per month with a {first['deductible']} deductible.",
            ),
            queued("status", text=f"{first['insurer']} call completed · quote captured"),
        ]
        for index, carrier in enumerate(carriers[1:], start=2):
            events.extend(
                [
                    queued("status", text=f"Calling simulated insurer {index} of {len(carriers)} · {carrier['insurer']}", company=carrier["insurer"]),
                    queued("message", speaker="representative", company=carrier["insurer"], text=f"Based on the sample profile, the quote is {carrier['monthly_premium']} per month with {carrier['personal_property']} in personal property coverage."),
                    queued("message", speaker="relay", text="Thank you. I’ve recorded the premium, limits, deductible, and loss-of-use coverage."),
                    queued("status", text=f"{carrier['insurer']} call completed · quote captured"),
                ]
            )
        task["quotes"] = deepcopy(carriers)
        events.append(queued("comparison", text=f"{len(carriers)} simulated quotes collected. Relay has not ranked or recommended them."))
        prompt = {
            "kind": "insurer_selection",
            "question": "Review the factual comparison and choose which insurer Relay should call back.",
            "options": [{"value": quote["id"], "label": quote["insurer"]} for quote in carriers],
        }
        self._schedule(task, events, prompt, "select_insurer")

    def _call_interruption(self, task: dict[str, Any], instruction: str) -> None:
        if task["stage"] == "takeover":
            raise InvalidAction("Return control to Relay before sending an instruction.")
        self._append(task, "message", speaker="user_private", text=instruction)
        lower = instruction.lower()
        if "multi" in lower and "discount" in lower:
            relay_text = "Before we continue, Alex would also like to know whether a multi-policy discount is available."
            rep_text = "Potentially, yes. It depends on the other policy and carrier, so I’ll mark that for confirmation."
        elif "bicycle" in lower or "bike" in lower:
            relay_text = "One more question from Alex: does this quote cover bicycle theft away from the apartment?"
            rep_text = "The sample policy covers bicycle theft, subject to the deductible and policy limits."
        else:
            relay_text = f"Before we continue, Alex asked me to clarify this: {instruction}"
            rep_text = "I’ve noted that question. In this simulation, the representative would provide the applicable policy detail."
        inserted = [
            queued("message", speaker="relay", text=relay_text),
            queued("message", speaker="representative", company=self._current_company(task), text=rep_text),
        ]
        if task["auto_advance"]:
            task["_pending"] = inserted + task["_pending"]
        else:
            self._schedule(task, inserted, deepcopy(task["prompt"]), task["stage"])

    def _handle_selection(self, task: dict[str, Any], value: str) -> None:
        selected = next((quote for quote in task["quotes"] if quote["id"] == value), None)
        if selected is None:
            raise InvalidAction("Choose one of the quoted insurers.")
        task["selected_insurer"] = selected["insurer"]
        self._append(task, "message", speaker="user_private", text=f"Continue with {selected['insurer']}.")
        self._append(task, "message", speaker="relay_private", text=f"You selected {selected['insurer']}. I need your approval before placing the application callback.")
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
            self._append(task, "status", text="Task stopped before application callback · no purchase made")
            task.update(phase="complete", stage="complete", status="complete", prompt=None)
            return
        if value != "approve":
            raise InvalidAction("Approve the callback or stop the task.")
        insurer = task["selected_insurer"]
        self._append(task, "message", speaker="user_private", text="Approved. Place the callback.")
        events = [
            queued("status", text=f"Calling {insurer} · continuing simulated application", company=insurer),
            queued("message", speaker="representative", company=insurer, text="Welcome back. I found the quote. Are you ready to continue with the application?"),
            queued("message", speaker="relay", text="Yes. Alex selected this quote and approved this callback. Alex will personally confirm the application details."),
        ]
        prompt = {
            "kind": "approval",
            "question": "The simulated representative asks you to confirm that the sample application details are accurate.",
            "options": [
                {"value": "confirm", "label": "I confirm"},
                {"value": "takeover", "label": "Take over"},
            ],
        }
        self._schedule(task, events, prompt, "confirm_application")

    def _handle_application_confirmation(self, task: dict[str, Any], value: str) -> None:
        if value not in {"confirm", "takeover"}:
            raise InvalidAction("Confirm the sample details or take over.")
        if value == "takeover":
            self._takeover(task)
            return
        self._append(task, "message", speaker="user_private", text="I confirm the sample application details are accurate.")
        events = [
            queued("message", speaker="relay", text="Alex confirms the sample application details are accurate."),
            queued("message", speaker="representative", company=task["selected_insurer"], text="Thank you. The final step is the simulated payment."),
        ]
        prompt = {
            "kind": "payment_method",
            "question": "Choose how to handle the simulated payment segment. Use fake card data only.",
            "options": [
                {"value": "takeover", "label": "Take over (recommended)"},
                {"value": "local_tts", "label": "Use local device voice"},
                {"value": "risky", "label": "Let Relay speak it · risky"},
            ],
        }
        self._schedule(task, events, prompt, "payment_method")

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
            task["prompt"] = self._payment_prompt()
            return
        if value != "continue":
            raise InvalidAction("Continue with fake data or choose a safer option.")
        self._enter_secure_mode(task, "risky")

    def _enter_secure_mode(self, task: dict[str, Any], method: str) -> None:
        task["secure_mode"] = True
        labels = {"takeover": "User takeover", "local_tts": "Local device voice", "risky": "Relay voice simulation"}
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
        self._append(task, "message", speaker="relay", text=f"Done. The simulated policy with {task['selected_insurer']} is active. I saved the non-sensitive outcome and visible transcript.")
        self._append(task, "status", text="Task complete · simulated confirmation captured")
        task.update(phase="complete", stage="complete", status="complete", prompt=None)

    def _takeover(self, task: dict[str, Any]) -> None:
        if task["phase"] != "calling" or task["status"] == "complete":
            raise InvalidAction("Takeover is available only during the live-call stage.")
        if task["stage"] == "takeover":
            raise InvalidAction("You already have the call.")
        task["_resume"] = {
            "stage": task["stage"],
            "prompt": deepcopy(task["prompt"]),
            "status": task["status"],
            "auto_advance": task["auto_advance"],
            "pending": deepcopy(task["_pending"]),
            "pending_prompt": deepcopy(task["_pending_prompt"]),
            "pending_stage": task["_pending_stage"],
        }
        task["_pending"] = []
        task["auto_advance"] = False
        self._append(task, "message", speaker="user_private", text="Take over the active call now.")
        self._append(task, "message", speaker="relay", text="Can you give me just one moment? Alex is taking over the call now.")
        self._append(task, "status", text="User takeover active · Relay microphone paused")
        task["stage"] = "takeover"
        task["status"] = "waiting_for_user"
        task["prompt"] = {
            "kind": "approval",
            "question": "You have the simulated call. Return control to Relay when ready.",
            "options": [{"value": "resume", "label": "Return control to Relay"}],
        }

    def _handle_resume(self, task: dict[str, Any], value: str) -> None:
        if value != "resume":
            raise InvalidAction("Return control to Relay to continue.")
        self._append(task, "message", speaker="user_private", text="Relay, you can continue.")
        self._append(task, "status", text="Relay resumed the simulated call")
        resume = task.pop("_resume")
        task["stage"] = resume["stage"]
        task["prompt"] = resume["prompt"]
        task["status"] = resume["status"]
        task["auto_advance"] = resume["auto_advance"]
        task["_pending"] = resume["pending"]
        task["_pending_prompt"] = resume["pending_prompt"]
        task["_pending_stage"] = resume["pending_stage"]

    def _schedule(
        self,
        task: dict[str, Any],
        events: list[dict[str, Any]],
        prompt: dict[str, Any] | None,
        next_stage: str,
    ) -> None:
        task["_pending"] = events
        task["_pending_prompt"] = deepcopy(prompt)
        task["_pending_stage"] = next_stage
        task["prompt"] = None
        task["status"] = "running"
        task["auto_advance"] = bool(events)
        if not events:
            self._finish_schedule(task)

    def _advance(self, task: dict[str, Any]) -> None:
        if not task["auto_advance"]:
            raise InvalidAction("There is no scripted turn waiting to advance.")
        event = task["_pending"].pop(0)
        event_type = event.pop("type")
        self._append(task, event_type, **event)
        if not task["_pending"]:
            self._finish_schedule(task)

    def _finish_schedule(self, task: dict[str, Any]) -> None:
        task["auto_advance"] = False
        task["stage"] = task["_pending_stage"]
        task["prompt"] = task["_pending_prompt"]
        task["status"] = "waiting_for_user" if task["prompt"] else "running"
        task["_pending_prompt"] = None
        task["_pending_stage"] = None

    def _selected_quotes(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        return [quote for carrier_id in task["carrier_ids"] for quote in QUOTES if quote["id"] == carrier_id]

    def _current_company(self, task: dict[str, Any]) -> str:
        for event in reversed(task["events"]):
            if event.get("company"):
                return event["company"]
        return task.get("selected_insurer") or self._selected_quotes(task)[0]["insurer"]

    def _payment_prompt(self) -> dict[str, Any]:
        return {
            "kind": "payment_method",
            "question": "Choose a safer simulated payment method.",
            "options": [
                {"value": "takeover", "label": "Take over (recommended)"},
                {"value": "local_tts", "label": "Use local device voice"},
                {"value": "risky", "label": "Let Relay speak it · risky"},
            ],
        }

    def _append(self, task: dict[str, Any], event_type: str, **payload: Any) -> None:
        event = {
            "id": len(task["events"]) + 1,
            "type": event_type,
            "phase": task["phase"],
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        task["events"].append(event)
        event_name = "transcript.turn" if event_type == "message" else f"workflow.{event_type}"
        self._events.append(event_name, {"task_id": task["id"], **payload})
