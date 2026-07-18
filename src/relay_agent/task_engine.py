from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from relay_agent.event_log import EventLog
from relay_agent.task_store import SQLiteTaskStore


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


def secure_field_prompt(field: str, simulated: bool = True) -> dict[str, Any]:
    fields = {
        "card_number": {
            "label": "Card number",
            "placeholder": "Fake card: 4242 4242 4242 4242",
            "input_mode": "numeric",
        },
        "expiration": {
            "label": "Expiration date",
            "placeholder": "Fake expiration: 12/34",
            "input_mode": "numeric",
        },
        "cvv": {
            "label": "Security code",
            "placeholder": "Fake CVV: 123",
            "input_mode": "numeric",
        },
        "full_ssn": {
            "label": "Full Social Security number",
            "placeholder": "Fake SSN: 000-00-0000",
            "input_mode": "numeric",
        },
        "ssn_last_four": {
            "label": "Last four digits of Social Security number",
            "placeholder": "Fake last four: 0000",
            "input_mode": "numeric",
        },
        "date_of_birth": {
            "label": "Date of birth",
            "placeholder": "Choose a fake date",
            "input_mode": "date",
        },
    }
    if field not in fields:
        raise InvalidAction("Unsupported secure field.")
    spec = fields[field]
    context = "representative" if simulated else "live representative"
    return {
        "kind": "secure_field",
        "field": field,
        "question": (
            f"The {context} asked only for: {spec['label']}. Enter fake test data; "
            "Relay and cloud transcription remain paused until local speech finishes."
        ),
        "label": spec["label"],
        "placeholder": spec["placeholder"],
        "input_mode": spec["input_mode"],
        "options": [],
    }


def queued(event_type: str, **payload: Any) -> dict[str, Any]:
    return {"type": event_type, **payload}


class DeterministicTaskEngine:
    """An interruptible deterministic workflow for validating Relay's UX."""

    def __init__(
        self,
        events: EventLog,
        store: SQLiteTaskStore | None = None,
        namespace: str = "demo",
    ) -> None:
        self._store = store
        self._namespace = namespace
        self._tasks = store.load_all(namespace) if store else {}
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
            candidate = next(
                (context.get("address_candidate") for context in task["contexts"] if context.get("address_candidate")),
                None,
            )
            if candidate:
                task["address_candidate"] = candidate
                task["stage"] = "confirm_address"
                task["prompt"] = self._address_confirmation_prompt(candidate)
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text=f"I found this possible property address in the PDF: {candidate}. Please confirm it before I use it.",
                )
            else:
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text="I read the PDF but could not identify a reliable property address. Please type the full address below.",
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
            self._persist(task)
        self._events.append("task.created", {"task_id": task_id, "goal": cleaned_goal, "phase": "planning"})
        return self._snapshot(task)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            return self._snapshot(task)

    def attach_context(self, task_id: str, context: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if task["phase"] != "planning":
                raise InvalidAction("Supporting documents can be added during the planning stage.")
            task["contexts"].append(deepcopy(context))
            self._append(task, "status", text=f"Local PDF context attached · {context['filename']}")
            candidate = context.get("address_candidate")
            if candidate and task["stage"] in {"plan_address", "confirm_address"}:
                task["address_candidate"] = candidate
                task["stage"] = "confirm_address"
                task["prompt"] = self._address_confirmation_prompt(candidate)
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text=f"I found this possible property address in the PDF: {candidate}. Please confirm it before I use it.",
                )
            elif task["stage"] == "plan_address":
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text="I read the PDF but could not identify a reliable property address. Please type the full address below.",
                )
            self._persist(task)
            snapshot = self._snapshot(task)
        self._events.append("task.context_attached", {"task_id": task_id, **context})
        return snapshot

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
            self._persist(task)
            snapshot = self._snapshot(task)

        if action != "advance":
            self._events.append(
                "task.action",
                {"task_id": task_id, "action": action, "value": value, "stage": snapshot["stage"]},
            )
        return snapshot

    def _snapshot(self, task: dict[str, Any]) -> dict[str, Any]:
        return deepcopy({key: value for key, value in task.items() if not key.startswith("_")})

    def _persist(self, task: dict[str, Any]) -> None:
        if self._store:
            self._store.save(self._namespace, task)

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
            if not self._looks_like_address(instruction):
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text=(
                        "That does not look like a complete property address, so I will not use it. "
                        "Type the street address or attach the lease PDF here."
                    ),
                )
                return
            task["address"] = instruction
            self._append(
                task,
                "message",
                speaker="relay_private",
                text="Thanks. I’ll use that address for this simulated quote task. Here is the proposed call plan.",
            )
            self._present_plan(task)
            return

        if task["stage"] == "confirm_address":
            if self._looks_like_address(instruction):
                task.pop("address_candidate", None)
                task["address"] = instruction
                self._append(task, "message", speaker="relay_private", text="Thanks. I’ll use the address you typed and prepare the call plan.")
                self._present_plan(task)
                return
            self._append(
                task,
                "message",
                speaker="relay_private",
                text="Please use the confirmation buttons, type a complete street address, or attach another PDF.",
            )
            return

        if task["stage"] in {"select_insurer", "approve_callback"}:
            self._append(
                task,
                "message",
                speaker="relay_private",
                text=(
                    "I’ve noted that in our decision conversation. Review the factual table and choose a carrier "
                    "when ready; I will not place the callback without your approval."
                ),
            )
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
            "confirm_address": self._handle_address_confirmation,
            "claims_history": self._handle_claims,
            "select_insurer": self._handle_selection,
            "approve_callback": self._handle_callback_approval,
            "confirm_application": self._handle_application_confirmation,
            "payment_method": self._handle_payment_method,
            "risky_confirmation": self._handle_risky_confirmation,
            "secure_card_number": self._handle_local_tts_field,
            "secure_expiration": self._handle_local_tts_field,
            "secure_cvv": self._handle_local_tts_field,
            "secure_complete": self._handle_secure_complete,
            "takeover": self._handle_resume,
        }
        handler = handlers.get(task["stage"])
        if handler is None:
            raise InvalidAction("Relay is not waiting for that answer right now.")
        handler(task, value)

    def _handle_address_confirmation(self, task: dict[str, Any], value: str) -> None:
        if value == "use_address":
            task["address"] = task.pop("address_candidate")
            self._append(task, "message", speaker="user_private", text=f"Use {task['address']}.")
            self._append(task, "message", speaker="relay_private", text="Confirmed. Here is the proposed call plan.")
            self._present_plan(task)
            return
        if value != "type_address":
            raise InvalidAction("Confirm the extracted address or choose to type another one.")
        task.pop("address_candidate", None)
        task["stage"] = "plan_address"
        task["prompt"] = {
            "kind": "text_reply",
            "question": "Type the full rented property address below or attach another PDF.",
            "options": [],
        }
        self._append(task, "message", speaker="user_private", text="That is not the correct address.")
        self._append(task, "message", speaker="relay_private", text="Understood. Please type the correct full address or attach another PDF.")

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
                    queued("message", speaker="representative", company=carrier["insurer"], text=f"{carrier['insurer']}, this is Jordan. How can I help you today?"),
                    queued(
                        "message",
                        speaker="relay",
                        text=(
                            "Hi, I’m Relay, an AI voice assistant speaking for Alex, who is following by text. "
                            "Alex would like a renters-insurance quote and will personally provide facts and decisions. Is that okay?"
                        ),
                    ),
                    queued("message", speaker="representative", company=carrier["insurer"], text="Yes, I can help with that quote."),
                    queued(
                        "message",
                        speaker="relay",
                        text=(
                            f"The property is {task['address']}. Alex’s answer on claims in the last five years is "
                            f"{answer.lower()}. Please quote the same sample coverage profile so we can compare it consistently."
                        ),
                    ),
                    queued("message", speaker="representative", company=carrier["insurer"], text=f"Based on that information, the quote is {carrier['monthly_premium']} per month with {carrier['personal_property']} in personal property coverage."),
                    queued("message", speaker="relay", text="Thank you. I’ve recorded the premium, limits, deductible, and loss-of-use coverage."),
                    queued("status", text=f"{carrier['insurer']} call completed · quote captured"),
                ]
            )
        task["quotes"] = deepcopy(carriers)
        self._schedule(task, events, None, "quote_calls_complete")

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
        self._append(task, "message", speaker="relay_private", text="Approved. I’m returning to the live-call panel for the application callback.")
        task["phase"] = "calling"
        events = [
            queued("status", text=f"Calling {insurer} · continuing simulated application", company=insurer),
            queued("message", speaker="representative", company=insurer, text=f"{insurer}, this is Sam. How can I help?"),
            queued(
                "message",
                speaker="relay",
                text=(
                    "Hi, I’m Relay, an AI voice assistant speaking for Alex, who is following by text. "
                    f"Alex previously received a renters quote from {insurer}, selected it, and approved this callback "
                    "to continue the application. Alex will personally confirm facts and decisions. Is that okay?"
                ),
            ),
            queued("message", speaker="representative", company=insurer, text="Yes. I found the simulated quote and can continue the application."),
            queued("message", speaker="relay", text="Thank you. Alex is ready to confirm the sample application details."),
        ]
        prompt = {
            "kind": "approval",
            "question": "The simulated representative asks you to confirm that the sample application details are accurate.",
            "options": [
                {"value": "confirm", "label": "I confirm"},
                {"value": "takeover", "label": "Simulate takeover · no audio"},
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
                {"value": "takeover", "label": "Simulate takeover · no audio"},
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
        if value == "local_tts":
            self._start_local_tts(task)
            return
        if value != "takeover":
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
        labels = {"takeover": "Simulated takeover (no audio)", "local_tts": "Local device voice", "risky": "Relay voice simulation"}
        self._append(task, "message", speaker="relay", text="One moment—Alex will handle the payment details through a protected channel.")
        self._append(task, "status", text=f"Secure mode active · {labels[method]} · cloud transcription paused")
        self._append(task, "secure_gap", text="Sensitive payment segment is neither transcribed nor logged.")
        task["stage"] = "secure_complete"
        task["prompt"] = {
            "kind": "secure_complete",
            "method": method,
            "question": "Complete the simulated protected payment segment when ready.",
            "options": [{"value": "complete", "label": "Complete simulated payment"}],
        }

    def _start_local_tts(self, task: dict[str, Any]) -> None:
        task["secure_mode"] = True
        self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="Please provide the card number first.")
        self._append(task, "message", speaker="relay", text="One moment while Alex provides only the card number through the local secure voice channel.")
        self._append(task, "status", text="Relay audio paused · waiting for local device voice: card number")
        self._append(task, "secure_gap", text="Card number is handled locally and is not sent to Relay’s server, model, transcript, or log.")
        task["stage"] = "secure_card_number"
        task["prompt"] = self._secure_field_prompt("card_number")

    def _handle_local_tts_field(self, task: dict[str, Any], value: str) -> None:
        if value != "sent":
            raise InvalidAction("The local voice channel must signal completion before Relay resumes.")
        completed_stage = task["stage"]
        task["secure_mode"] = False
        self._append(task, "status", text="Local device voice completed · Relay returned to the line")
        self._append(task, "message", speaker="relay", text="Thanks. Please continue.")

        if completed_stage == "secure_card_number":
            self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="Thank you. What is the expiration date?")
            self._begin_next_secure_field(task, "expiration", "expiration date")
            return
        if completed_stage == "secure_expiration":
            self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="And what is the three-digit security code?")
            self._begin_next_secure_field(task, "cvv", "security code")
            return

        self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="The sandbox payment was accepted. The sample policy is active immediately.")
        self._append(task, "message", speaker="relay", text="Thank you. I’ve captured the non-sensitive confirmation for Alex.")
        task["phase"] = "planning"
        self._append(task, "message", speaker="relay_private", text=f"The simulated purchase with {task['selected_insurer']} is complete. No payment fields entered Relay’s context, transcript, or logs.")
        self._append(task, "status", text="Task complete · simulated confirmation captured")
        task.update(phase="complete", stage="complete", status="complete", prompt=None)

    def _begin_next_secure_field(self, task: dict[str, Any], field: str, spoken_label: str) -> None:
        self._append(task, "message", speaker="relay", text=f"One moment while Alex provides only the {spoken_label} through the local secure voice channel.")
        self._append(task, "status", text=f"Relay audio paused · waiting for local device voice: {spoken_label}")
        self._append(task, "secure_gap", text=f"The {spoken_label} is handled locally and is not transcribed or logged.")
        task["secure_mode"] = True
        task["stage"] = f"secure_{field}"
        task["prompt"] = self._secure_field_prompt(field)

    def _handle_secure_complete(self, task: dict[str, Any], value: str) -> None:
        if value != "complete":
            raise InvalidAction("Complete the simulated secure segment to continue.")
        task["secure_mode"] = False
        self._append(task, "status", text="Secure mode ended · Relay reconnected · transcription resumed")
        self._append(task, "message", speaker="representative", company=task["selected_insurer"], text="The sandbox payment was accepted. The sample policy is active immediately.")
        self._append(task, "message", speaker="relay", text="Thank you. I’ve captured the non-sensitive confirmation for Alex.")
        task["phase"] = "planning"
        self._append(task, "message", speaker="relay_private", text=f"The simulated purchase with {task['selected_insurer']} is complete. I saved only the non-sensitive outcome and visible transcript.")
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
        self._append(task, "message", speaker="user_private", text="Simulate taking over the active call.")
        self._append(task, "message", speaker="relay", text="Can you give me just one moment? Alex would take over the live audio here.")
        self._append(task, "status", text="Takeover simulation active · no microphone or telephone audio is connected in this build")
        task["stage"] = "takeover"
        task["status"] = "waiting_for_user"
        task["prompt"] = {
            "kind": "approval",
            "question": "This deterministic preview only pauses the script; it does not connect your microphone. Resume when ready.",
            "options": [{"value": "resume", "label": "Resume simulated Relay"}],
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
        if task["_pending_stage"] == "quote_calls_complete":
            task["_pending_prompt"] = None
            task["_pending_stage"] = None
            task["phase"] = "planning"
            task["stage"] = "select_insurer"
            task["status"] = "waiting_for_user"
            self._append(
                task,
                "message",
                speaker="relay_private",
                text="I finished the quote calls. We’re back in our planning conversation; here is the factual comparison.",
            )
            self._append(
                task,
                "comparison",
                text=f"{len(task['quotes'])} simulated quotes collected. Relay has not ranked or recommended them.",
            )
            task["prompt"] = {
                "kind": "insurer_selection",
                "question": "Review the comparison and choose an insurer, or type questions and instructions below.",
                "options": [{"value": quote["id"], "label": quote["insurer"]} for quote in task["quotes"]],
            }
            return
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
                {"value": "takeover", "label": "Simulate takeover · no audio"},
                {"value": "local_tts", "label": "Use local device voice"},
                {"value": "risky", "label": "Let Relay speak it · risky"},
            ],
        }

    def _secure_field_prompt(self, field: str) -> dict[str, Any]:
        return secure_field_prompt(field)

    def _address_confirmation_prompt(self, candidate: str) -> dict[str, Any]:
        return {
            "kind": "approval",
            "question": f"Use {candidate} as the rented property address?",
            "options": [
                {"value": "use_address", "label": "Use this address"},
                {"value": "type_address", "label": "Type another address"},
            ],
        }

    def _looks_like_address(self, value: str) -> bool:
        if not re.search(r"\d", value):
            return False
        return bool(
            re.search(
                r"\b(street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|lane|ln\.?|drive|dr\.?|court|ct\.?)\b",
                value,
                re.IGNORECASE,
            )
        )

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
