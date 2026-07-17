from types import SimpleNamespace

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from relay_agent.app import create_app
from relay_agent.credentials import CredentialStore, RelayCredentials
from relay_agent.planner import PlanAction, PlanningTurn
from relay_agent.telephony import TelephonyService
from relay_agent.tunnel import TunnelManager


class FakeCalls:
    def __init__(self):
        self.arguments = None

    def create(self, **arguments):
        self.arguments = arguments
        return SimpleNamespace(sid="CA123", status="queued")


class ExecutablePlanner:
    ready = True
    model = "test-planner"

    def plan(self, goal, messages, contexts):
        return PlanningTurn(
            status="plan_ready",
            message="The call is ready for review.",
            plan_summary="Call the verified service line.",
            actions=[
                PlanAction(
                    kind="phone_call",
                    label="Call service",
                    purpose="Request a factual service quote.",
                    target="Example Provider",
                    needs_lookup=False,
                    phone_number="+12025550199",
                    contact_source_url="https://example.com/contact",
                )
            ],
        )


def configured_store(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save(
        RelayCredentials(
            twilio_account_sid="ACtest",
            twilio_auth_token="test-auth-token",
            twilio_from_number="+12025550123",
            openai_api_key="sk-test",
        )
    )
    return store


def test_lazy_tunnel_url_is_used_for_per_call_webhooks(tmp_path):
    launches = []
    terminations = []
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: launches.append(port) or SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=terminations.append,
    )
    calls = FakeCalls()
    credentials = configured_store(tmp_path).resolve
    service = TelephonyService(credentials, tunnel, lambda account_sid, auth_token: SimpleNamespace(calls=calls))

    assert tunnel.active is False
    result = service.place_call("+12025550199")

    assert result == {"sid": "CA123", "status": "queued"}
    assert launches == [8765]
    assert calls.arguments["url"] == "https://relay.trycloudflare.com/api/twilio/voice"
    assert calls.arguments["status_callback"] == "https://relay.trycloudflare.com/api/twilio/status"
    assert calls.arguments["from_"] == "+12025550123"
    tunnel.release()
    assert terminations == [8765]


def test_task_identity_is_attached_to_per_call_webhooks(tmp_path):
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=lambda port: None,
    )
    calls = FakeCalls()
    service = TelephonyService(
        configured_store(tmp_path).resolve,
        tunnel,
        lambda account_sid, auth_token: SimpleNamespace(calls=calls),
    )

    service.place_call("+12025550199", "task-123", 2)

    assert calls.arguments["url"].endswith("/api/twilio/voice?task_id=task-123&queue_index=2")
    assert calls.arguments["status_callback"].endswith("/api/twilio/status?task_id=task-123&queue_index=2")


def test_approved_agentic_plan_places_the_verified_call(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_MODE", "standard")
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    calls = FakeCalls()
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=lambda port: None,
    )
    client = TestClient(
        create_app(
            planner=ExecutablePlanner(),
            credential_store=configured_store(tmp_path),
            tunnel_manager=tunnel,
            twilio_client_factory=lambda account_sid, auth_token: SimpleNamespace(calls=calls),
        )
    )
    task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()

    approved = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "approve"},
    )

    assert approved.status_code == 200
    assert approved.json()["phase"] == "calling"
    assert calls.arguments["to"] == "+12025550199"
    assert f"task_id={task['id']}" in calls.arguments["url"]


def test_twilio_webhook_signature_accepts_valid_and_rejects_invalid_or_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_MODE", "standard")
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    store = configured_store(tmp_path)
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=lambda port: None,
    )
    tunnel.acquire()
    client = TestClient(create_app(credential_store=store, tunnel_manager=tunnel))
    url = "https://relay.trycloudflare.com/api/twilio/voice?task_id=task-123&queue_index=0"
    parameters = {"CallSid": "CA123", "From": "+12025550199"}
    signature = RequestValidator("test-auth-token").compute_signature(url, parameters)

    path = "/api/twilio/voice?task_id=task-123&queue_index=0"
    valid = client.post(path, data=parameters, headers={"X-Twilio-Signature": signature})
    invalid = client.post(path, data=parameters, headers={"X-Twilio-Signature": "invalid"})
    missing = client.post(path, data=parameters)

    assert valid.status_code == 200
    assert valid.headers["content-type"].startswith("application/xml")
    assert "wss://relay.trycloudflare.com/api/twilio/media" in valid.text
    assert 'name="task_id" value="task-123"' in valid.text
    assert invalid.status_code == 403
    assert missing.status_code == 403
