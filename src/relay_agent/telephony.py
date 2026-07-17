from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from twilio.request_validator import RequestValidator

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
    ) -> None:
        self._credentials = credentials
        self.tunnel = tunnel
        self._client_factory = client_factory or create_twilio_client

    def place_call(self, to: str) -> dict[str, str]:
        credentials = self._credentials()
        if not credentials.complete:
            raise RuntimeError("Relay telephony credentials are incomplete.")
        self.tunnel.acquire()
        voice_url = self.tunnel.url("/api/twilio/voice")
        status_url = self.tunnel.url("/api/twilio/status")
        try:
            client = self._client_factory(credentials.twilio_account_sid, credentials.twilio_auth_token)
            call = client.calls.create(
                to=to,
                from_=credentials.twilio_from_number,
                url=voice_url,
                method="POST",
                status_callback=status_url,
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
        except Exception:
            self.tunnel.release()
            raise
        return {"sid": call.sid, "status": str(call.status or "queued")}
