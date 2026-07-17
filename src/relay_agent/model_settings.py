from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from relay_agent.event_log import default_data_dir


PLANNING_MODELS = ("gpt-5.4-mini", "gpt-5.4", "gpt-5.6")
REALTIME_MODELS = ("gpt-realtime-2.1-mini", "gpt-realtime-2.1")
TRANSCRIPTION_MODELS = ("gpt-4o-mini-transcribe", "gpt-4o-transcribe")


@dataclass(frozen=True)
class ModelSettings:
    planning_model: str = PLANNING_MODELS[0]
    realtime_model: str = REALTIME_MODELS[0]
    transcription_model: str = TRANSCRIPTION_MODELS[0]

    def validate(self) -> None:
        allowed = {
            "planning_model": PLANNING_MODELS,
            "realtime_model": REALTIME_MODELS,
            "transcription_model": TRANSCRIPTION_MODELS,
        }
        for name, choices in allowed.items():
            value = getattr(self, name)
            if value not in choices:
                raise ValueError(f"Unsupported {name.replace('_', ' ')}: {value}")


def default_model_settings_path() -> Path:
    return default_data_dir() / "model-settings.json"


class ModelSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_model_settings_path()

    def load(self) -> ModelSettings:
        if not self.path.exists():
            return ModelSettings()
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ModelSettings()
        if not isinstance(values, dict):
            return ModelSettings()
        allowed = {field.name for field in fields(ModelSettings)}
        settings = ModelSettings(**{key: str(value) for key, value in values.items() if key in allowed})
        try:
            settings.validate()
        except ValueError:
            return ModelSettings()
        return settings

    def save(self, settings: ModelSettings) -> None:
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(asdict(settings), output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
