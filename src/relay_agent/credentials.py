from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Mapping


REQUIRED_CREDENTIALS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "OPENAI_API_KEY",
)


@dataclass(frozen=True)
class RelayCredentials:
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    openai_api_key: str = ""

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "RelayCredentials":
        return cls(
            twilio_account_sid=values.get("TWILIO_ACCOUNT_SID", "").strip(),
            twilio_auth_token=values.get("TWILIO_AUTH_TOKEN", "").strip(),
            twilio_from_number=values.get("TWILIO_FROM_NUMBER", "").strip(),
            openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
        )

    def as_environment(self) -> dict[str, str]:
        return {
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_FROM_NUMBER": self.twilio_from_number,
            "OPENAI_API_KEY": self.openai_api_key,
        }

    @property
    def missing(self) -> list[str]:
        values = self.as_environment()
        return [name for name in REQUIRED_CREDENTIALS if not values[name]]

    @property
    def complete(self) -> bool:
        return not self.missing


def default_credentials_path() -> Path:
    configured = os.environ.get("RELAY_DATA_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".relay"
    return root / "credentials.json"


class CredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_credentials_path()

    def load(self) -> RelayCredentials:
        if not self.path.exists():
            return RelayCredentials()
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return RelayCredentials()
        if not isinstance(values, dict):
            return RelayCredentials()
        allowed = {field.name for field in fields(RelayCredentials)}
        return RelayCredentials(**{key: str(value or "") for key, value in values.items() if key in allowed})

    def resolve(self, environment: Mapping[str, str] | None = None) -> RelayCredentials:
        stored = self.load().as_environment()
        current = environment if environment is not None else os.environ
        merged = {name: current.get(name, "").strip() or stored[name] for name in REQUIRED_CREDENTIALS}
        return RelayCredentials.from_mapping(merged)

    def save(self, credentials: RelayCredentials, require_complete: bool = True) -> None:
        if require_complete and not credentials.complete:
            raise ValueError(f"Missing required credentials: {', '.join(credentials.missing)}")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(asdict(credentials), output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
