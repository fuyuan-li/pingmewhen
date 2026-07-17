import stat

from fastapi.testclient import TestClient

import relay_agent.app as app_module
from relay_agent.app import create_app
from relay_agent.model_settings import ModelSettings, ModelSettingsStore


class FakeOpenAIPlanner:
    ready = True

    def __init__(self, api_key, model):
        self.model = model

    def plan(self, goal, messages, contexts):
        raise AssertionError("This settings test should not call the planner.")


def configure_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_MODE", "standard")
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12025550123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(app_module, "OpenAIPlanner", FakeOpenAIPlanner)


def test_model_settings_store_defaults_persists_and_reloads(tmp_path):
    path = tmp_path / "model-settings.json"
    store = ModelSettingsStore(path)

    assert store.load() == ModelSettings()

    selected = ModelSettings(
        planning_model="gpt-5.6",
        realtime_model="gpt-realtime-2.1",
        transcription_model="gpt-4o-transcribe",
    )
    store.save(selected)

    assert store.load() == selected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_dashboard_model_settings_update_runtime_and_reject_unknown_models(monkeypatch, tmp_path):
    configure_runtime(monkeypatch, tmp_path)
    path = tmp_path / "machine" / "model-settings.json"
    client = TestClient(create_app(model_settings_store=ModelSettingsStore(path)))

    defaults = client.get("/api/model-settings").json()
    assert defaults["planning_model"] == "gpt-5.4-mini"
    assert defaults["realtime_model"] == "gpt-realtime-2.1-mini"
    assert defaults["transcription_model"] == "gpt-4o-mini-transcribe"
    assert defaults["options"]["planning"] == ["gpt-5.4-mini", "gpt-5.4", "gpt-5.6"]

    saved = client.put(
        "/api/model-settings",
        json={
            "planning_model": "gpt-5.4",
            "realtime_model": "gpt-realtime-2.1",
            "transcription_model": "gpt-4o-transcribe",
        },
    )

    assert saved.status_code == 200
    runtime = client.get("/api/runtime").json()
    assert runtime["planner_model"] == "gpt-5.4"
    assert runtime["realtime_model"] == "gpt-realtime-2.1"
    assert runtime["transcription_model"] == "gpt-4o-transcribe"
    assert ModelSettingsStore(path).load().planning_model == "gpt-5.4"

    rejected = client.put(
        "/api/model-settings",
        json={
            "planning_model": "made-up-model",
            "realtime_model": "gpt-realtime-2.1-mini",
            "transcription_model": "gpt-4o-mini-transcribe",
        },
    )
    assert rejected.status_code == 422
    assert "Unsupported planning model" in rejected.json()["detail"]
