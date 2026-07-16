from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any


SENSITIVE_KEYS = {
    "account_pin",
    "api_key",
    "authorization",
    "card_number",
    "credit_card",
    "cvv",
    "full_ssn",
    "password",
    "security_code",
    "ssn",
    "token",
}


def default_data_dir() -> Path:
    configured = os.environ.get("RELAY_DATA_DIR")
    return Path(configured).expanduser() if configured else Path.cwd() / ".relay"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class EventLog:
    """Append-only local JSONL event log with recursive key redaction."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "logs" / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, event: str, payload: dict[str, Any] | None = None) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "payload": redact(payload or {}),
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as output:
            output.write(encoded + "\n")
