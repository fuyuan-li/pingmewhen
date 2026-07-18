from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

from twilio.request_validator import RequestValidator

from relay_agent.call_capabilities import CallCapabilityStore
from relay_agent.credentials import RelayCredentials
from relay_agent.tunnel import TunnelManager


TERMINAL_CALL_STATUSES = {"busy", "canceled", "completed", "failed", "no-answer"}


def create_twilio_client(account_sid: str, auth_token: str) -> Any:
    from twilio.rest import Client

    return Client(account_sid, auth_token)


def validate_twilio_signature(auth_token: str, url: str, parameters: Mapping[str, Any], signature: str) -> bool:
    if not signature:
        return False
    return RequestValidator(auth_token).validate(url, parameters, signature)


class TelephonyService:
    def __init__(
        self,
        credentials: Callable[[], RelayCredentials],
        tunnel: TunnelManager,
        client_factory: Callable[[str, str], Any] | None = None,
        capabilities: CallCapabilityStore | None = None,
    ) -> None:
        self._credentials = credentials
        self.tunnel = tunnel
        self._client_factory = client_factory or create_twilio_client
        self.capabilities = capabilities or CallCapabilityStore()

    def place_call(self, to: str, task_id: str = "", queue_index: int = 0) -> dict[str, str]:
        credentials = self._credentials()
        if not credentials.complete:
            raise RuntimeError("Relay telephony credentials are incomplete.")
        self.tunnel.acquire()
        capability = self.capabilities.issue(task_id, queue_index, credentials.twilio_account_sid)
        voice_query = urlencode(
            {"capability": capability.voice_token, "task_id": task_id, "queue_index": queue_index}
        )
        status_query = urlencode(
            {"capability": capability.status_token, "task_id": task_id, "queue_index": queue_index}
        )
        voice_url = self.tunnel.url("/api/twilio/voice", voice_query)
        status_url = self.tunnel.url("/api/twilio/status", status_query)
        try:
            client = self._client_factory(credentials.twilio_account_sid, credentials.twilio_auth_token)
            call = client.calls.create(
                to=to,
                from_=credentials.twilio_from_number,
                url=voice_url,
                status_callback=status_url,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
            self.capabilities.bind(capability, call.sid)
        except Exception:
            self.capabilities.discard(capability)
            self.tunnel.release()
            raise
        return {"sid": call.sid, "status": str(call.status or "queued")}

    def end_call(self, call_sid: str) -> str:
        """Hang up a live Twilio call. Twilio then fires its 'completed' status callback, which drives the
        normal call-teardown path (media stop, capability revocation, task completion)."""
        if not call_sid:
            raise RuntimeError("Cannot end a call without a call SID.")
        credentials = self._credentials()
        if not credentials.complete:
            raise RuntimeError("Relay telephony credentials are incomplete.")
        client = self._client_factory(credentials.twilio_account_sid, credentials.twilio_auth_token)
        call = client.calls(call_sid).update(status="completed")
        return str(getattr(call, "status", "") or "completed")
