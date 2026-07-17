from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from relay_agent.event_log import default_data_dir


def call_debug_enabled() -> bool:
    return os.environ.get("RELAY_DEBUG_CALL_CONTEXT", "").strip().lower() in {"1", "true", "yes", "on"}


class CallDebugTrace:
    def __init__(self, task_id: str, call_sid: str) -> None:
        self.path: Path | None = None
        self._lock = Lock()
        if not call_debug_enabled():
            return
        directory = default_data_dir() / "debug" / "calls"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        safe_task = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:100]
        safe_call = re.sub(r"[^A-Za-z0-9_-]", "_", call_sid)[:100]
        self.path = directory / f"{safe_task}-{safe_call}.jsonl"

    def append(self, event: str, payload: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "payload": payload,
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                output.write(encoded + "\n")
            self.path.chmod(0o600)
