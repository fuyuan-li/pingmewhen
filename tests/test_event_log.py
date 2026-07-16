import json

from relay_agent.event_log import EventLog, default_data_dir


def test_event_log_redacts_sensitive_values(tmp_path):
    path = tmp_path / "events.jsonl"
    events = EventLog(path)

    events.append(
        "secure_mode.test",
        {
            "card_number": "4242424242424242",
            "nested": {"full_ssn": "111-22-3333", "safe": "visible"},
        },
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["payload"]["card_number"] == "[REDACTED]"
    assert record["payload"]["nested"]["full_ssn"] == "[REDACTED]"
    assert record["payload"]["nested"]["safe"] == "visible"


def test_default_data_dir_is_local_to_working_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert default_data_dir() == tmp_path / ".relay"
