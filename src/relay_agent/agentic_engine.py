from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from relay_agent.event_log import EventLog
from relay_agent.planner import Planner, PlannerError, PlanningTurn
from relay_agent.task_engine import InvalidAction, TaskNotFound
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
            if event["type"] != "message" or event.get("speaker") not in {"user_private", "relay_private"}:
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
        self._append(
            task,
            "status",
            text="Plan approved · external execution queued, but no telephony connector is enabled in this build",
        )
        self._append(
            task,
            "message",
            speaker="relay_private",
            text=(
                "I saved your approval and queued the external actions. This production slice does not yet have a "
                "telephony connector, so I will stop here instead of simulating a real call."
            ),
        )
        task.update(stage="execution_ready", status="waiting_for_execution", prompt=None)

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
