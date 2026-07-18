from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from threading import Lock


CAPABILITY_QUERY_PATTERN = re.compile(r"(?P<prefix>(?:capability|voice_token|status_token)=)[^&\s]+")
MEDIA_CAPABILITY_PATTERN = re.compile(r"(?P<prefix>/api/twilio/(?:media|listen)/)[A-Za-z0-9_-]+")


def redact_capabilities(value: str) -> str:
    redacted = CAPABILITY_QUERY_PATTERN.sub(r"\g<prefix>[REDACTED]", value)
    return MEDIA_CAPABILITY_PATTERN.sub(r"\g<prefix>[REDACTED]", redacted)


class CapabilityAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_capabilities(item) if isinstance(item, str) else item for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_capabilities(item) if isinstance(item, str) else item
                for key, item in record.args.items()
            }
        if isinstance(record.msg, str):
            record.msg = redact_capabilities(record.msg)
        return True


@dataclass
class CallCapability:
    task_id: str
    queue_index: int
    account_sid: str
    voice_token: str
    status_token: str
    media_token: str
    listen_token: str
    call_sid: str = ""
    active: bool = True


class CallCapabilityStore:
    def __init__(self) -> None:
        self._records: list[CallCapability] = []
        self._lock = Lock()

    def issue(self, task_id: str, queue_index: int, account_sid: str) -> CallCapability:
        record = CallCapability(
            task_id=task_id,
            queue_index=queue_index,
            account_sid=account_sid,
            voice_token=secrets.token_urlsafe(32),
            status_token=secrets.token_urlsafe(32),
            media_token=secrets.token_urlsafe(32),
            listen_token=secrets.token_urlsafe(32),
        )
        with self._lock:
            self._records.append(record)
        return record

    def bind(self, record: CallCapability, call_sid: str) -> None:
        with self._lock:
            if record not in self._records or not record.active:
                raise RuntimeError("The call capability is no longer active.")
            record.call_sid = call_sid

    def authenticate(
        self,
        scope: str,
        token: str,
        account_sid: str = "",
        call_sid: str = "",
    ) -> CallCapability | None:
        if scope not in {"voice", "status", "media", "listen"} or not token:
            return None
        token_attribute = f"{scope}_token"
        with self._lock:
            for record in self._records:
                if not record.active or not secrets.compare_digest(getattr(record, token_attribute), token):
                    continue
                if account_sid and not secrets.compare_digest(record.account_sid, account_sid):
                    return None
                if call_sid and (not record.call_sid or not secrets.compare_digest(record.call_sid, call_sid)):
                    return None
                return record
        return None

    def active_for_task(self, task_id: str, call_sid: str) -> CallCapability | None:
        with self._lock:
            for record in self._records:
                if not record.active:
                    continue
                if secrets.compare_digest(record.task_id, task_id) and secrets.compare_digest(record.call_sid, call_sid):
                    return record
        return None

    def revoke(self, call_sid: str) -> None:
        with self._lock:
            for record in self._records:
                if record.active and secrets.compare_digest(record.call_sid, call_sid):
                    record.active = False

    def discard(self, record: CallCapability) -> None:
        with self._lock:
            record.active = False
