import json
import stat

from fastapi.testclient import TestClient

from relay_agent.app import create_app
from relay_agent.credentials import CredentialStore, RelayCredentials


def test_credential_store_persists_reloads_and_prefers_environment(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    saved = RelayCredentials(
        twilio_account_sid="ACstored",
        twilio_auth_token="stored-token",
        twilio_from_number="+12025550123",
        openai_api_key="sk-stored",
    )

    store.save(saved)

    assert store.load() == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.resolve({"OPENAI_API_KEY": "sk-environment"}).openai_api_key == "sk-environment"


def test_dashboard_onboarding_saves_missing_credentials_without_returning_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "machine" / "credentials.json"
    client = TestClient(create_app(credential_store=CredentialStore(path)))

    runtime = client.get("/api/runtime").json()
    assert runtime["setup_required"] is True
    assert set(runtime["missing_credentials"]) == {
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "OPENAI_API_KEY",
    }

    response = client.post(
        "/api/setup",
        json={
            "twilio_account_sid": "ACuser",
            "twilio_auth_token": "local-auth-token",
            "twilio_from_number": "+12025550123",
            "openai_api_key": "sk-local",
        },
    )

    assert response.status_code == 200
    assert "local-auth-token" not in response.text
    assert "sk-local" not in response.text
    assert client.get("/api/runtime").json()["setup_required"] is False
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["twilio_auth_token"] == "local-auth-token"
    assert persisted["openai_api_key"] == "sk-local"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_onboarding_does_not_copy_environment_credentials_into_local_file(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACenvironment")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "environment-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12025550123")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "machine" / "credentials.json"
    client = TestClient(create_app(credential_store=CredentialStore(path)))

    response = client.post("/api/setup", json={"openai_api_key": "sk-entered"})

    assert response.status_code == 200
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted == {
        "twilio_account_sid": "",
        "twilio_auth_token": "",
        "twilio_from_number": "",
        "openai_api_key": "sk-entered",
    }
