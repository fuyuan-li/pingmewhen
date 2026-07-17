from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import re
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from relay_agent.event_log import EventLog
from relay_agent.planner import Planner, PlannerError, PlanningTurn
from relay_agent.task_engine import InvalidAction, TaskNotFound, secure_field_prompt
from relay_agent.task_store import SQLiteTaskStore


class AgenticTaskEngine:
    """Model-driven private planning with hard application-owned approvals."""

    def __init__(
        self,
        events: EventLog,
        planner: Planner,
        store: SQLiteTaskStore,
        context_reader: Callable[[str], str],
    ) -> None:
        self._events = events
        self._planner = planner
        self._store = store
        self._context_reader = context_reader
        self._tasks = store.load_all("production")
        self._lock = Lock()

    def create(self, goal: str, contexts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        cleaned_goal = goal.strip()
        if not cleaned_goal:
            raise InvalidAction("Describe what Relay should accomplish.")
        task = {
            "id": uuid4().hex,
            "goal": cleaned_goal,
            "contexts": deepcopy(contexts or []),
            "phase": "planning",
            "status": "running",
            "stage": "agent_planning",
            "created_at": datetime.now(UTC).isoformat(),
            "events": [],
            "quotes": [],
            "prompt": None,
            "secure_mode": False,
            "auto_advance": False,
            "approved_plan": None,
            "execution_queue": [],
            "current_call": None,
            "call_state": None,
            "secure_expected_field": None,
            "secure_fields_completed": [],
        }
        self._append(task, "message", speaker="user_private", text=cleaned_goal)
        self._run_planner(task)
        with self._lock:
            self._tasks[task["id"]] = task
            self._store.save("production", task)
        self._events.append("task.created", {"task_id": task["id"], "goal": cleaned_goal, "mode": "production"})
        return self._snapshot(task)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            return self._snapshot(task)

    def attach_context(self, task_id: str, context: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task["status"] == "complete" or task["stage"] == "execution_ready":
                raise InvalidAction("Supporting documents can be added before the plan is approved.")
            before = deepcopy(task)
            task["contexts"].append(deepcopy(context))
            self._append(task, "status", text=f"Local PDF context attached · {context['filename']}")
            try:
                self._run_planner(task)
            except PlannerError:
                task.clear()
                task.update(before)
                raise
            self._store.save("production", task)
            snapshot = self._snapshot(task)
        self._events.append("task.context_attached", {"task_id": task_id, **context})
        return snapshot

    def act(self, task_id: str, action: str, value: str = "") -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if action == "instruction":
                instruction = value.strip()
                if not instruction:
                    raise InvalidAction("Type a message before sending it.")
                if task["status"] == "complete" or task["stage"] == "execution_ready":
                    raise InvalidAction("This approved plan is no longer editable.")
                if task["phase"] == "calling":
                    if task.get("secure_mode"):
                        raise InvalidAction("Relay is paused during the protected exchange. Do not type sensitive data here.")
                    self._append(task, "message", speaker="user_private", text=instruction)
                    self._append(task, "status", text="Private instruction sent to the active call")
                    self._store.save("production", task)
                    snapshot = self._snapshot(task)
                    self._events.append(
                        "task.action",
                        {"task_id": task_id, "action": action, "value": value, "stage": task["stage"]},
                    )
                    return snapshot
                before = deepcopy(task)
                self._append(task, "message", speaker="user_private", text=instruction)
                try:
                    self._run_planner(task)
                except PlannerError:
                    task.clear()
                    task.update(before)
                    raise
            elif action == "answer":
                self._answer(task, value)
            else:
                raise InvalidAction("Production planning supports private instructions and approval responses.")
            self._store.save("production", task)
            snapshot = self._snapshot(task)
        self._events.append("task.action", {"task_id": task_id, "action": action, "value": value, "stage": task["stage"]})
        return snapshot

    def _run_planner(self, task: dict[str, Any]) -> None:
        task.update(status="running", stage="agent_planning", prompt=None)
        messages = []
        for event in task["events"]:
            if event["type"] != "message" or event.get("speaker") not in {
                "user_private",
                "relay_private",
                "relay",
                "representative",
            }:
                continue
            speaker = event.get("speaker")
            if speaker in {"relay", "representative"}:
                messages.append(
                    {
                        "role": "user",
                        "content": f"EXTERNAL CALL TRANSCRIPT — {speaker}: {event['text']}",
                    }
                )
                continue
            messages.append(
                {"role": "user" if event["speaker"] == "user_private" else "assistant", "content": event["text"]}
            )
        contexts = [
            {
                "filename": context.get("filename", "context.pdf"),
                "text": self._context_reader(context.get("id", "")),
            }
            for context in task["contexts"]
        ]
        turn = self._planner.plan(task["goal"], messages, contexts)
        self._append(task, "message", speaker="relay_private", text=turn.message)
        if turn.status == "plan_ready":
            self._present_plan(task, turn)
            return
        task.update(status="waiting_for_user", stage="collecting_context")
        question = turn.questions[0] if turn.questions else "What information should Relay use to continue planning?"
        task["prompt"] = {"kind": "text_reply", "question": question, "options": []}

    def _present_plan(self, task: dict[str, Any], turn: PlanningTurn) -> None:
        plan = turn.model_dump()
        task["current_plan"] = plan
        self._append(
            task,
            "agent_plan",
            text="Proposed task plan",
            summary=turn.plan_summary,
            actions=[action.model_dump() for action in turn.actions],
            questions=turn.questions,
            assumptions=turn.assumptions,
        )
        task.update(status="waiting_for_user", stage="plan_review")
        task["prompt"] = {
            "kind": "approval",
            "question": "Approve this plan, hold to revise it, or decline. Relay will not execute an external action without approval.",
            "options": [
                {"value": "approve", "label": "Approve plan"},
                {"value": "hold", "label": "Hold · revise"},
                {"value": "decline", "label": "Decline"},
            ],
        }

    def _answer(self, task: dict[str, Any], value: str) -> None:
        if task["stage"] != "plan_review":
            raise InvalidAction("Relay is not waiting for an approval response.")
        if value == "hold":
            self._append(task, "message", speaker="user_private", text="Hold. I want to revise the plan.")
            self._append(task, "message", speaker="relay_private", text="Tell me what to change and I will prepare a revised plan.")
            task["prompt"] = {"kind": "text_reply", "question": "Type your plan changes below.", "options": []}
            return
        if value == "decline":
            self._append(task, "message", speaker="user_private", text="Decline this plan.")
            self._append(task, "status", text="Plan declined · no external action started")
            task.update(phase="complete", stage="complete", status="complete", prompt=None)
            return
        if value != "approve":
            raise InvalidAction("Approve, hold, or decline the plan.")
        task["approved_plan"] = deepcopy(task.get("current_plan"))
        self._append(task, "message", speaker="user_private", text="Approved. Proceed with this plan.")
        actions = (task.get("approved_plan") or {}).get("actions", [])
        phone_actions = [action for action in actions if action.get("kind") == "phone_call"]
        executable = [
            action
            for action in phone_actions
            if re.fullmatch(r"\+[1-9]\d{7,14}", action.get("phone_number", ""))
            and action.get("contact_source_url", "").startswith(("https://", "http://"))
            and not action.get("needs_lookup", True)
        ]
        if phone_actions and len(executable) != len(phone_actions):
            self._append(task, "status", text="Plan approved · dialing blocked because no verified phone number is present")
            self._append(
                task,
                "message",
                speaker="relay_private",
                text=(
                    "I cannot safely dial this plan yet because its phone-call actions do not contain exact verified "
                    "E.164 numbers. I will revise the plan to resolve the contacts before asking you to approve again."
                ),
            )
            task.update(stage="execution_blocked", status="waiting_for_user", prompt=None)
            return
        if not phone_actions:
            self._append(task, "status", text="Plan approved · no supported external phone action was present")
            self._append(
                task,
                "message",
                speaker="relay_private",
                text="This build can execute approved phone actions, but this plan contains no phone call to run.",
            )
            task.update(stage="execution_blocked", status="waiting_for_user", prompt=None)
            return
        task["execution_queue"] = [
            {"index": index, "action": deepcopy(action), "status": "pending", "call_sid": None}
            for index, action in enumerate(executable)
        ]
        self._append(task, "status", text=f"Plan approved · {len(executable)} external call(s) queued")
        task.update(stage="execution_ready", status="waiting_for_execution", prompt=None)

    def next_phone_action(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._require(task_id)
            if task.get("current_call"):
                return None
            return deepcopy(next((item for item in task.get("execution_queue", []) if item["status"] == "pending"), None))

    def begin_call(self, task_id: str, queue_index: int, call_sid: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            item = self._queue_item(task, queue_index)
            if item["status"] != "pending":
                raise InvalidAction("This call action is no longer pending.")
            item.update(status="active", call_sid=call_sid)
            task["current_call"] = {"queue_index": queue_index, "call_sid": call_sid}
            task.update(
                phase="calling",
                stage="calling",
                status="running",
                prompt=None,
                call_state="DIALING",
                secure_mode=False,
                secure_expected_field=None,
                secure_fields_completed=[],
            )
            action = item["action"]
            self._append(task, "status", text=f"Calling {action['target']} · real Twilio call", company=action["target"])
            self._store.save("production", task)
            return self._snapshot(task)

    def append_transcript(self, task_id: str, speaker: str, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if not cleaned:
            return self.get(task_id)
        if speaker not in {"relay", "representative"}:
            raise InvalidAction("Unsupported transcript speaker.")
        with self._lock:
            task = self._require(task_id)
            if task.get("secure_mode"):
                return self._snapshot(task)
            target = ""
            if task.get("current_call"):
                target = self._queue_item(task, task["current_call"]["queue_index"])["action"].get("target", "")
            self._append(task, "message", speaker=speaker, text=cleaned, company=target)
            self._store.save("production", task)
            return self._snapshot(task)

    def mark_call_connected(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("current_call") and not task.get("secure_mode"):
                task["call_state"] = "CONNECTED"
                self._store.save("production", task)
            return self._snapshot(task)

    def request_secure_field(self, task_id: str, field_name: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task["phase"] != "calling" or not task.get("current_call"):
                raise InvalidAction("Secure local voice requires an active call.")
            task["secure_mode"] = True
            task["secure_expected_field"] = field_name
            if field_name == "verification_request" or field_name in task.get("secure_fields_completed", []):
                task.update(call_state="HUMAN_TAKEOVER", stage="human_takeover", status="waiting_for_user")
                task["prompt"] = {
                    "kind": "takeover_required",
                    "question": (
                        "The representative requested a protected field again. Relay and cloud transcription remain "
                        "disconnected. Human takeover is required; Relay will not repeat the value."
                    ),
                    "options": [],
                }
                self._append(task, "status", text="Human takeover required · repeated protected-field request")
                self._store.save("production", task)
                return self._snapshot(task)
            task["call_state"] = "SECURE_HANDOFF_PENDING"
            self._append(task, "status", text=f"Secure handoff pending · {field_name}")
            task.update(call_state="SECURE_LOCAL", stage=f"secure_{field_name}", status="waiting_for_user")
            task["prompt"] = secure_field_prompt(field_name, simulated=False)
            self._append(task, "secure_gap", text="Protected audio and transcript content are suppressed for this field.")
            self._store.save("production", task)
            return self._snapshot(task)

    def complete_secure_field(self, task_id: str, field_name: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if (
                task.get("call_state") != "SECURE_LOCAL"
                or not task.get("secure_mode")
                or task.get("secure_expected_field") != field_name
            ):
                raise InvalidAction("Relay is not waiting for that secure field.")
            completed = task.setdefault("secure_fields_completed", [])
            if field_name not in completed:
                completed.append(field_name)
            task.update(
                secure_mode=False,
                secure_expected_field=None,
                call_state="CONNECTED",
                stage="calling",
                status="running",
                prompt=None,
            )
            self._append(task, "status", text="Local secure voice completed · Relay returned to the line")
            self._store.save("production", task)
            return self._snapshot(task)

    def resume_from_takeover(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("call_state") != "HUMAN_TAKEOVER":
                raise InvalidAction("Relay can resume only after human takeover.")
            task.update(
                secure_mode=False,
                secure_expected_field=None,
                call_state="CONNECTED",
                stage="calling",
                status="running",
                prompt=None,
            )
            self._append(task, "status", text="Human takeover ended · Relay returned to the active call")
            self._store.save("production", task)
            return self._snapshot(task)

    def finish_call(self, task_id: str, call_sid: str, status: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            item = next((entry for entry in task.get("execution_queue", []) if entry.get("call_sid") == call_sid), None)
            if item is None or item["status"] != "active":
                return self._snapshot(task)
            item["status"] = "complete" if status == "completed" else status
            task["current_call"] = None
            task.update(
                secure_mode=False,
                secure_expected_field=None,
                call_state="COMPLETED" if status == "completed" else "FAILED",
            )
            self._append(task, "status", text=f"Call with {item['action']['target']} ended · {status}")
            if any(entry["status"] == "pending" for entry in task.get("execution_queue", [])):
                task.update(phase="calling", stage="execution_ready", status="waiting_for_execution", prompt=None)
            else:
                task.update(phase="planning", stage="post_call_review", status="waiting_for_user", prompt=None)
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text="The approved calls are complete. I retained their transcripts in this task and am ready to review the results with you.",
                )
                task["prompt"] = {
                    "kind": "text_reply",
                    "question": "Ask Relay to summarize the results or give the next instruction.",
                    "options": [],
                }
            self._store.save("production", task)
            return self._snapshot(task)

    def fail_execution(self, task_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            task["current_call"] = None
            task.update(phase="planning", stage="execution_failed", status="waiting_for_user")
            self._append(task, "status", text=f"Approved call could not start · {reason}")
            task["prompt"] = {"kind": "text_reply", "question": "Revise the plan or try again.", "options": []}
            self._store.save("production", task)
            return self._snapshot(task)

    def call_context(self, task_id: str, queue_index: int) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            item = self._queue_item(task, queue_index)
            return {
                "goal": task["goal"],
                "action": deepcopy(item["action"]),
                "private_messages": [
                    event["text"]
                    for event in task["events"]
                    if event["type"] == "message" and event.get("speaker") == "user_private"
                ],
                "document_context": "\n\n".join(
                    self._context_reader(context.get("id", ""))
                    for context in task.get("contexts", [])
                    if context.get("id")
                )[:12000],
                "prior_call_transcript": "\n".join(
                    f"{event.get('speaker')}: {event['text']}"
                    for event in task["events"]
                    if event["type"] == "message" and event.get("speaker") in {"relay", "representative"}
                )[-12000:],
            }

    def _queue_item(self, task: dict[str, Any], queue_index: int) -> dict[str, Any]:
        item = next((entry for entry in task.get("execution_queue", []) if entry["index"] == queue_index), None)
        if item is None:
            raise InvalidAction("Call action not found.")
        return item

    def _require(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        return task

    def _snapshot(self, task: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(task)

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
