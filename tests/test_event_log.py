import json
from pathlib import Path

from relay_agent.event_log import EventLog, default_data_dir


def test_event_log_redacts_sensitive_values(tmp_path):
    path = tmp_path / "events.jsonl"
    events = EventLog(path)

    events.append(
        "secure_mode.test",
        {
            "card_number": "4242424242424242",
            "nested": {
                "full_ssn": "111-22-3333",
                "twilio_auth_token": "twilio-secret",
                "openai_api_key": "openai-secret",
                "safe": "visible",
            },
        },
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["payload"]["card_number"] == "[REDACTED]"
    assert record["payload"]["nested"]["full_ssn"] == "[REDACTED]"
    assert record["payload"]["nested"]["twilio_auth_token"] == "[REDACTED]"
    assert record["payload"]["nested"]["openai_api_key"] == "[REDACTED]"
    assert record["payload"]["nested"]["safe"] == "visible"


def test_default_data_dir_is_under_home_not_current_working_directory(monkeypatch, tmp_path):
    home = tmp_path / "home"
    working_directory = tmp_path / "unrelated-project"
    working_directory.mkdir()
    monkeypatch.delenv("RELAY_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(working_directory)

    assert default_data_dir() == home / ".relay"
    assert default_data_dir() != working_directory / ".relay"


def test_relay_data_dir_still_overrides_home(monkeypatch, tmp_path):
    configured = tmp_path / "configured-data"
    monkeypatch.setenv("RELAY_DATA_DIR", str(configured))

    assert default_data_dir() == configured
