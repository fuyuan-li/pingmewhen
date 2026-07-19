from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
import re
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from relay_agent.event_log import EventLog
from relay_agent.interaction_prompts import user_input_prompt
from relay_agent.local_tts import sensitive_input_spec
from relay_agent.names import normalize_display_name
from relay_agent.planner import Planner, PlannerError, PlanningTurn
from relay_agent.task_engine import InvalidAction, TaskNotFound
from relay_agent.task_store import SQLiteTaskStore


def normalize_phone_number(value: str) -> str:
    return re.sub(r"[\s().-]", "", value)


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
        for task in self._tasks.values():
            task.setdefault("context_updates", [])
            task.setdefault("takeover_active", False)
            task.setdefault("takeover_sensitive", False)
        self._lock = Lock()

    def create(self, goal: str, contexts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        cleaned_goal = goal.strip()
        if not cleaned_goal:
            raise InvalidAction("Describe what PingMeWhen should accomplish.")
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
            "takeover_active": False,
            "takeover_sensitive": False,
            "caller_name": "",
            "context_updates": [],
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
                        raise InvalidAction("PingMeWhen is paused during the protected exchange. Do not type sensitive data here.")
                    self._append(task, "message", speaker="user_private", text=instruction)
                    if task.get("call_state") == "WAITING_FOR_USER":
                        task.update(call_state="CONNECTED", stage="calling", status="running", prompt=None)
                        self._append(task, "status", text="User answer delivered · PingMeWhen returned to the conversation")
                    else:
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
        caller_name = str(task.get("caller_name", "")).strip()
        if caller_name:
            messages.append(
                {
                    "role": "developer",
                    "content": (
                        "RELAY TASK STATE: The caller display name is already confirmed as "
                        f"{json.dumps(caller_name)}. Preserve it and do not ask for it again."
                    ),
                }
            )
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
        confirmed_name = turn.caller_name.strip()
        if confirmed_name:
            task["caller_name"] = normalize_display_name(confirmed_name)[:100]
        if turn.status == "plan_ready":
            has_phone_actions = any(action.kind == "phone_call" for action in turn.actions)
            if has_phone_actions and not task.get("caller_name"):
                task.update(status="waiting_for_user", stage="collecting_context")
                question = next((question.strip() for question in turn.questions if question.strip()), "")
                if not question:
                    recent_user_text = "\n".join(
                        event["text"]
                        for event in task["events"]
                        if event["type"] == "message" and event.get("speaker") == "user_private"
                    )
                    question = (
                        "在拨号前，我该怎么介绍你？告诉我你希望我在说“我是代表某某打来的”时使用的名字就可以，"
                        "名字或昵称都行。"
                        if re.search(r"[\u3400-\u9fff]", recent_user_text)
                        else (
                            "Before I dial, how should I introduce you? A first name or nickname is completely fine; "
                            "I will use it to say I am calling on your behalf."
                        )
                    )
                self._append(task, "message", speaker="relay_private", text=question)
                task["prompt"] = user_input_prompt(question)
                return
            self._append(task, "message", speaker="relay_private", text=turn.message)
            self._present_plan(task, turn)
            return
        self._append(task, "message", speaker="relay_private", text=turn.message)
        task.update(status="waiting_for_user", stage="collecting_context")
        question = turn.questions[0] if turn.questions else "What information should PingMeWhen use to continue planning?"
        task["prompt"] = user_input_prompt(question)

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
            "question": "Approve this plan, hold to revise it, or decline. PingMeWhen will not execute an external action without approval.",
            "options": [
                {"value": "approve", "label": "Approve plan"},
                {"value": "hold", "label": "Hold · revise"},
                {"value": "decline", "label": "Decline"},
            ],
        }

    def _answer(self, task: dict[str, Any], value: str) -> None:
        if task["stage"] != "plan_review":
            raise InvalidAction("PingMeWhen is not waiting for an approval response.")
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
        executable = []
        blocked = []
        for action in phone_actions:
            supplied_number = str(action.get("phone_number", "")).strip()
            normalized_number = normalize_phone_number(supplied_number)
            action["phone_number"] = normalized_number
            reasons = []
            if not normalized_number:
                reasons.append("phone number is missing")
            elif not re.fullmatch(r"\+[1-9]\d{7,14}", normalized_number):
                reasons.append(f'phone number "{supplied_number}" does not resolve to valid E.164')
            if action.get("needs_lookup", True):
                reasons.append("contact verification is still marked as requiring lookup")
            contact_provided_by = action.get("contact_provided_by", "research")
            if contact_provided_by == "research" and not action.get("contact_source_url", "").startswith(
                ("https://", "http://")
            ):
                reasons.append("official contact source URL is missing or invalid")
            elif contact_provided_by not in {"user", "research"}:
                reasons.append("contact source must identify the user or PingMeWhen research")
            if reasons:
                identity = action.get("label") or action.get("target") or "Unnamed phone call"
                target = action.get("target", "").strip()
                blocked.append(f"{identity}{f' ({target})' if target and target != identity else ''}: {', '.join(reasons)}")
            else:
                executable.append(action)
        if blocked:
            details = "; ".join(blocked)
            self._append(task, "status", text=f"Plan approved · dialing blocked · {details}")
            self._append(
                task,
                "message",
                speaker="relay_private",
                text=(
                    f"I cannot safely dial the following phone-call action(s): {details}. "
                    "Please revise those specific contact details before approving again."
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

    def mark_connection_starting(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("stage") != "execution_ready" or task.get("current_call"):
                raise InvalidAction("PingMeWhen is not ready to establish the call connection.")
            task.update(phase="planning", stage="connection_starting", status="running", prompt=None)
            self._append(task, "status", text="Checking secure call tunnel reachability…")
            self._store.save("production", task)
            return self._snapshot(task)

    def record_tunnel_health(self, task_id: str, healthy: bool) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("stage") != "connection_starting":
                raise InvalidAction("PingMeWhen is not checking the call tunnel.")
            self._append(
                task,
                "status",
                text=(
                    "Secure call tunnel reachability confirmed"
                    if healthy
                    else "Tunnel health check inconclusive · proceeding with the approved call as requested"
                ),
            )
            self._store.save("production", task)
            return self._snapshot(task)

    def mark_call_starting(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("stage") != "connection_starting":
                raise InvalidAction("PingMeWhen is not ready to start the approved call.")
            pending = next((item for item in task.get("execution_queue", []) if item["status"] == "pending"), None)
            target = (pending or {}).get("action", {}).get("target", "the approved destination")
            self._append(task, "status", text=f"Calling {target} through the secure call tunnel…")
            self._store.save("production", task)
            return self._snapshot(task)

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
            takeover_already_active = bool(task.get("takeover_active"))
            task.update(
                call_state="HUMAN_TAKEOVER",
                stage="human_takeover" if takeover_already_active else "takeover_required",
                status="waiting_for_user",
                takeover_active=takeover_already_active,
                takeover_sensitive=True,
            )
            repeated = field_name == "verification_request" or field_name in task.get("secure_fields_completed", [])
            task["prompt"] = {
                "kind": "takeover_required",
                "question": (
                    "You must take over now — click Take over and type a non-sensitive response. PingMeWhen will not "
                    "repeat the protected value, and cloud transcription is paused."
                    if repeated
                    else (
                        "You must take over now — click Take over to type this yourself. PingMeWhen and cloud "
                        "transcription are paused for this protected exchange."
                    )
                ),
                "options": [],
                "field": field_name,
                "repeated": repeated,
                **sensitive_input_spec(field_name),
            }
            self._append(task, "status", text=f"Typed takeover required · protected {field_name} request")
            self._append(task, "secure_gap", text="Protected audio and transcript content are suppressed.")
            self._store.save("production", task)
            return self._snapshot(task)

    def begin_typed_takeover(self, task_id: str, sensitive: bool = False) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task["phase"] != "calling" or not task.get("current_call"):
                raise InvalidAction("Typed takeover requires an active call.")
            already_required = task.get("call_state") == "HUMAN_TAKEOVER" and task.get("stage") == "takeover_required"
            if sensitive and not already_required:
                raise InvalidAction("Protected takeover is not currently required.")
            if task.get("takeover_active"):
                raise InvalidAction("Typed takeover is already active.")
            task.update(
                call_state="HUMAN_TAKEOVER",
                stage="human_takeover",
                status="waiting_for_user",
                takeover_active=True,
                takeover_sensitive=bool(sensitive or task.get("takeover_sensitive")),
                prompt=task.get("prompt") if sensitive else None,
            )
            self._append(task, "status", text="Typed takeover started · PingMeWhen is silent")
            self._store.save("production", task)
            snapshot = self._snapshot(task)
        self._events.append(
            "call.takeover_started",
            {"task_id": task_id, "sensitive": snapshot["takeover_sensitive"]},
        )
        return snapshot

    def mark_takeover_speech(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("call_state") != "HUMAN_TAKEOVER" or not task.get("takeover_active"):
                raise InvalidAction("Type-to-speak is available only during an active takeover.")
            self._append(
                task,
                "status",
                text=(
                    "Protected value spoken locally"
                    if task.get("takeover_sensitive")
                    else "Your typed message was spoken locally to the representative"
                ),
                channel="private",
            )
            self._store.save("production", task)
            return self._snapshot(task)

    def request_user_input(
        self,
        task_id: str,
        question: str,
        input_kind: str,
        blocking: bool,
        interaction_id: str = "",
        reason: str = "missing_fact",
        representative_update: str = "",
    ) -> dict[str, Any]:
        cleaned_question = question.strip()
        with self._lock:
            task = self._require(task_id)
            if task["phase"] != "calling" or not task.get("current_call"):
                raise InvalidAction("User input can be requested only during an active call.")
            if task.get("secure_mode"):
                raise InvalidAction("Normal user input is unavailable during a protected exchange.")
            if not cleaned_question or input_kind != "text" or not isinstance(blocking, bool):
                raise InvalidAction("PingMeWhen requested unsupported user input.")
            task.update(call_state="WAITING_FOR_USER", stage="waiting_for_user", status="waiting_for_user")
            task["prompt"] = user_input_prompt(cleaned_question, blocking=blocking)
            if interaction_id:
                task["prompt"].update(interaction_id=interaction_id, reason=reason)
                task.setdefault("pending_interactions", []).append(
                    {
                        "id": interaction_id,
                        "reason": reason,
                        "representative_update": representative_update,
                        "question": cleaned_question,
                        "status": "pending",
                    }
                )
            self._append(task, "message", speaker="relay_private", text=cleaned_question)
            self._append(task, "status", text="PingMeWhen is waiting for your answer")
            self._store.save("production", task)
            return self._snapshot(task)

    def record_call_private_exchange(
        self,
        task_id: str,
        text: str,
        disposition: str,
        context_update: dict[str, Any] | None,
        private_reply: str,
        resumed_call: bool,
        interaction_id: str = "",
    ) -> dict[str, Any]:
        cleaned = text.strip()
        with self._lock:
            task = self._require(task_id)
            if task["phase"] != "calling" or not task.get("current_call"):
                raise InvalidAction("Private call messages require an active call.")
            if task.get("secure_mode"):
                raise InvalidAction("PingMeWhen is paused during the protected exchange. Do not type sensitive data here.")
            self._append(task, "message", speaker="user_private", text=cleaned, channel="private")
            if context_update is not None:
                task.setdefault("context_updates", []).append(deepcopy(context_update))
            if private_reply.strip():
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text=private_reply.strip(),
                    channel="private",
                )
            if resumed_call:
                if interaction_id:
                    for interaction in task.get("pending_interactions", []):
                        if interaction.get("id") == interaction_id and interaction.get("status") == "pending":
                            interaction.update(
                                status="resolved",
                                resolution=cleaned,
                                context_update_id=(context_update or {}).get("id", ""),
                            )
                            break
                task.update(call_state="CONNECTED", stage="calling", status="running", prompt=None)
                self._append(
                    task,
                    "status",
                    text="Your answer was added to PingMeWhen's confirmed call context",
                    channel="private",
                )
            elif disposition == "private_meta":
                self._append(task, "status", text="Kept private · nothing was spoken on the call", channel="private")
            else:
                self._append(
                    task,
                    "status",
                    text="Private direction added to PingMeWhen's confirmed call context",
                    channel="private",
                )
            self._store.save("production", task)
            snapshot = self._snapshot(task)
        self._events.append(
            "task.private_call_message",
            {
                "task_id": task_id,
                "disposition": disposition,
                "resumed_call": resumed_call,
                "interaction_id": interaction_id,
            },
        )
        return snapshot

    def record_call_delivery_failure(self, task_id: str, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        with self._lock:
            task = self._require(task_id)
            if task["phase"] != "calling" or not task.get("current_call"):
                raise InvalidAction("Private call messages require an active call.")
            self._append(task, "message", speaker="user_private", text=cleaned, channel="private")
            prompt = task.get("prompt") or {}
            pending_question = str(prompt.get("question", "")).strip()
            retry_text = "PingMeWhen could not apply that answer. Please send it again."
            if task.get("call_state") == "WAITING_FOR_USER" and pending_question:
                retry_text = f"PingMeWhen could not apply that answer. Please try again: {pending_question}"
            self._append(task, "message", speaker="relay_private", text=retry_text, channel="private")
            self._append(task, "status", text="Answer not applied · the call remains active", channel="private")
            self._store.save("production", task)
            snapshot = self._snapshot(task)
        self._events.append("task.private_call_message_retry_requested", {"task_id": task_id})
        return snapshot

    def call_sid_for(self, task_id: str) -> str:
        with self._lock:
            task = self._require(task_id)
            active = task.get("current_call") or {}
            return str(active.get("call_sid", ""))

    def hang_up_call_by_user(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task["phase"] != "calling" or not task.get("current_call"):
                raise InvalidAction("There is no active call to hang up.")
            call_sid = str(task["current_call"]["call_sid"])
            # A deliberate user hangup is a clean, connected completion regardless of what the call was waiting on.
            task["call_state"] = "CONNECTED"
            self._append(task, "message", speaker="user_private", text="End the call.", channel="private")
            self._append(task, "status", text="Ending the call at your request", channel="private")
            self._store.save("production", task)
        snapshot = self.finish_call(task_id, call_sid, "completed")
        self._events.append("task.call_ended_by_user", {"task_id": task_id})
        return snapshot

    def resume_from_takeover(
        self,
        task_id: str,
        context_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            if task.get("call_state") != "HUMAN_TAKEOVER" or not task.get("takeover_active"):
                raise InvalidAction("PingMeWhen can resume only after human takeover.")
            sensitive = bool(task.get("takeover_sensitive"))
            completed_field = task.get("secure_expected_field")
            if context_update is not None and not sensitive:
                task.setdefault("context_updates", []).append(deepcopy(context_update))
            if sensitive and completed_field and completed_field != "verification_request":
                completed = task.setdefault("secure_fields_completed", [])
                if completed_field not in completed:
                    completed.append(completed_field)
            task.update(
                secure_mode=False,
                secure_expected_field=None,
                takeover_active=False,
                takeover_sensitive=False,
                call_state="CONNECTED",
                stage="calling",
                status="running",
                prompt=None,
            )
            self._append(task, "status", text="Human takeover ended · PingMeWhen returned to the active call")
            self._store.save("production", task)
            snapshot = self._snapshot(task)
        self._events.append("call.takeover_ended", {"task_id": task_id, "sensitive": sensitive})
        return snapshot

    def finish_call(self, task_id: str, call_sid: str, status: str) -> dict[str, Any]:
        with self._lock:
            task = self._require(task_id)
            item = next((entry for entry in task.get("execution_queue", []) if entry.get("call_sid") == call_sid), None)
            if item is None or item["status"] != "active":
                return self._snapshot(task)
            connected = task.get("call_state") in {
                "CONNECTED",
                "WAITING_FOR_USER",
                "HUMAN_TAKEOVER",
            }
            ended_while_waiting = task.get("call_state") == "WAITING_FOR_USER"
            successful = status == "completed" and connected and not ended_while_waiting
            item["status"] = "complete" if successful else "failed"
            task["current_call"] = None
            task.update(
                secure_mode=False,
                secure_expected_field=None,
                takeover_active=False,
                takeover_sensitive=False,
                call_state="COMPLETED" if successful else "FAILED",
            )
            if not successful:
                if status == "completed" and ended_while_waiting:
                    reason = "The call ended while PingMeWhen was waiting for your answer"
                elif status == "completed" and not connected:
                    reason = "PingMeWhen never connected to the call audio"
                else:
                    reason = f"Twilio ended the call with status {status}"
                self._append(task, "status", text=f"Call with {item['action']['target']} failed · {reason}")
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text=(
                        "The phone call ended before PingMeWhen completed the conversation. Review the retained transcript "
                        "and call status before deciding whether to retry."
                        if connected
                        else (
                            "The phone call ended before PingMeWhen completed a conversation. No conversation transcript "
                            "was captured. Check the call connection error before retrying."
                        )
                    ),
                )
                task.update(phase="planning", stage="execution_failed", status="waiting_for_user")
                task["prompt"] = {
                    "kind": "text_reply",
                    "question": "Review the connection failure, revise the plan, or ask PingMeWhen to retry.",
                    "options": [],
                }
                self._store.save("production", task)
                return self._snapshot(task)
            self._append(task, "status", text=f"Call with {item['action']['target']} ended · completed")
            if any(entry["status"] == "pending" for entry in task.get("execution_queue", [])):
                task.update(phase="calling", stage="execution_ready", status="waiting_for_execution", prompt=None)
            else:
                task.update(phase="planning", stage="post_call_review", status="waiting_for_user", prompt=None)
                completed_targets = [
                    entry["action"]["target"]
                    for entry in task.get("execution_queue", [])
                    if entry.get("status") == "completed"
                ]
                representative_turns = [
                    event["text"]
                    for event in task["events"]
                    if event["type"] == "message"
                    and event.get("speaker") == "representative"
                ]
                confirmed_updates = [
                    str(update.get("summary", "")).strip()
                    for update in task.get("context_updates", [])
                    if str(update.get("summary", "")).strip()
                ]
                self._append(
                    task,
                    "message",
                    speaker="relay_private",
                    text="The approved calls are complete. Here is the call summary and retained transcript.",
                )
                self._append(
                    task,
                    "call_summary",
                    text="Post-call summary",
                    target=", ".join(completed_targets) or item["action"]["target"],
                    outcome="Call completed",
                    highlights=representative_turns[-5:],
                    confirmed=confirmed_updates[-5:],
                    next_step="Review the outcome, ask a follow-up, or tell PingMeWhen what to do next.",
                )
                task["prompt"] = {
                    "kind": "text_reply",
                    "question": "Ask a follow-up or give PingMeWhen the next instruction.",
                    "options": [],
                    "input_kind": "text",
                    "placeholder": "Ask about the call or give the next instruction…",
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
                "caller_name": task.get("caller_name", ""),
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
                "context_updates": deepcopy(task.get("context_updates", [])),
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
        channel = payload.pop("channel", None)
        if channel is None:
            speaker = payload.get("speaker")
            if speaker in {"user_private", "relay_private"} or task["phase"] == "planning":
                channel = "private"
            else:
                channel = "call"
        event = {
            "id": len(task["events"]) + 1,
            "type": event_type,
            "phase": task["phase"],
            "timestamp": datetime.now(UTC).isoformat(),
            "channel": channel,
            **payload,
        }
        task["events"].append(event)
        event_name = "transcript.turn" if event_type == "message" else f"workflow.{event_type}"
        self._events.append(event_name, {"task_id": task["id"], **payload})
