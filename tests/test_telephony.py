import json
import logging
from types import SimpleNamespace
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from relay_agent.app import create_app
from relay_agent.call_capabilities import (
    CapabilityAccessLogFilter,
    CallCapabilityStore,
    redact_capabilities,
)
from relay_agent.cli import relay_log_config
from relay_agent.credentials import CredentialStore, RelayCredentials
from relay_agent.planner import PlanAction, PlanningTurn
from relay_agent.realtime_bridge import RealtimeSessionHub
from relay_agent.telephony import TelephonyService
from relay_agent.tunnel import TunnelManager


class FakeCalls:
    def __init__(self):
        self.arguments = None
        self.updated = []

    def create(self, **arguments):
        self.arguments = arguments
        return SimpleNamespace(sid="CA123", status="queued")

    def __call__(self, call_sid):
        calls = self

        class _CallContext:
            def update(self, **kwargs):
                calls.updated.append((call_sid, kwargs))
                return SimpleNamespace(sid=call_sid, status=kwargs.get("status", "completed"))

        return _CallContext()


class ExecutablePlanner:
    ready = True
    model = "test-planner"

    def plan(self, goal, messages, contexts):
        return PlanningTurn(
            status="plan_ready",
            message="The call is ready for review.",
            plan_summary="Call the verified service line.",
            caller_name="Taylor",
            actions=[
                PlanAction(
                    kind="phone_call",
                    label="Call service",
                    purpose="Request a factual service quote.",
                    target="Example Provider",
                    needs_lookup=False,
                    phone_number="+12025550199",
                    contact_provided_by="research",
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


def wait_for_call(client, task_id, calls):
    for _ in range(200):
        task = client.get(f"/api/tasks/{task_id}").json()
        if calls.arguments is not None and task["phase"] == "calling":
            return task
        if task["stage"] == "execution_failed":
            return task
        time.sleep(0.01)
    raise AssertionError("The background call attempt did not finish in time.")


def test_tunnel_url_is_used_for_per_call_webhooks(tmp_path):
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
    voice_url = urlparse(calls.arguments["url"])
    status_url = urlparse(calls.arguments["status_callback"])
    assert voice_url._replace(query="").geturl() == "https://relay.trycloudflare.com/api/twilio/voice"
    assert status_url._replace(query="").geturl() == "https://relay.trycloudflare.com/api/twilio/status"
    assert len(parse_qs(voice_url.query)["capability"][0]) >= 32
    assert len(parse_qs(status_url.query)["capability"][0]) >= 32
    assert parse_qs(voice_url.query)["capability"] != parse_qs(status_url.query)["capability"]
    assert calls.arguments["from_"] == "+12025550123"
    assert "method" not in calls.arguments
    assert "status_callback_method" not in calls.arguments
    tunnel.release()
    assert terminations == [8765]


def test_end_call_hangs_up_the_twilio_call(tmp_path):
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=lambda port: None,
    )
    calls = FakeCalls()
    credentials = configured_store(tmp_path).resolve
    service = TelephonyService(credentials, tunnel, lambda account_sid, auth_token: SimpleNamespace(calls=calls))

    status = service.end_call("CA123")

    assert status == "completed"
    assert calls.updated == [("CA123", {"status": "completed"})]


def test_production_runtime_starts_tunnel_at_application_start_and_checks_health_on_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    launches = []
    terminations = []
    health_checks = []
    calls = FakeCalls()
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: launches.append(port) or SimpleNamespace(tunnel="https://warm.trycloudflare.com"),
        terminator=terminations.append,
    )
    app = create_app(
        planner=ExecutablePlanner(),
        credential_store=configured_store(tmp_path),
        tunnel_manager=tunnel,
        twilio_client_factory=lambda account_sid, auth_token: SimpleNamespace(calls=calls),
        tunnel_readiness_checker=lambda url: health_checks.append(url) or True,
        connection_status_delay=0,
    )

    with TestClient(app) as client:
        for _ in range(100):
            if tunnel.active:
                break
            time.sleep(0.01)

        assert launches == [8765]
        assert health_checks == []
        runtime = client.get("/api/runtime").json()
        assert runtime["tunnel_active"] is True
        task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()
        approved = client.post(
            f"/api/tasks/{task['id']}/actions",
            json={"action": "answer", "value": "approve"},
        )
        calling = wait_for_call(client, task["id"], calls)

        assert approved.status_code == 200
        assert approved.json()["stage"] == "connection_starting"
        assert launches == [8765]
        assert health_checks == ["https://warm.trycloudflare.com"]
        assert calling["phase"] == "calling"
        assert tunnel.active is True
        assert terminations == []

    assert tunnel.active is False
    assert terminations == [8765]


def test_failed_call_releases_only_call_lease_when_session_keeps_tunnel_warm(tmp_path):
    class FailingCalls:
        def create(self, **arguments):
            raise RuntimeError("Twilio rejected the call")

    terminations = []
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: SimpleNamespace(tunnel="https://warm.trycloudflare.com"),
        terminator=terminations.append,
    )
    tunnel.acquire()
    service = TelephonyService(
        configured_store(tmp_path).resolve,
        tunnel,
        lambda account_sid, auth_token: SimpleNamespace(calls=FailingCalls()),
    )

    with pytest.raises(RuntimeError, match="Twilio rejected"):
        service.place_call("+12025550199")

    assert tunnel.active is True
    assert terminations == []
    tunnel.stop()
    assert terminations == [8765]


def test_session_tunnel_lease_reuses_one_tunnel_across_calls(tmp_path):
    class SequencedCalls:
        def __init__(self):
            self.count = 0

        def create(self, **arguments):
            self.count += 1
            return SimpleNamespace(sid=f"CA{self.count}", status="queued")

    launches = []
    terminations = []
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: launches.append(port) or SimpleNamespace(tunnel="https://ready.trycloudflare.com"),
        terminator=terminations.append,
    )
    calls = SequencedCalls()
    service = TelephonyService(
        configured_store(tmp_path).resolve,
        tunnel,
        lambda account_sid, auth_token: SimpleNamespace(calls=calls),
    )

    tunnel.acquire()
    service.place_call("+12025550199", "task-1", 0)
    tunnel.release()
    assert tunnel.active is True
    service.place_call("+12025550200", "task-1", 1)
    tunnel.release()

    assert calls.count == 2
    assert launches == [8765]
    assert tunnel.active is True
    tunnel.release()
    assert terminations == [8765]


def test_inconclusive_tunnel_health_check_is_logged_but_call_is_still_attempted(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    launches = []
    calls = FakeCalls()
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: launches.append(port) or SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=lambda port: None,
    )
    client = TestClient(
        create_app(
            planner=ExecutablePlanner(),
            credential_store=configured_store(tmp_path),
            tunnel_manager=tunnel,
            twilio_client_factory=lambda account_sid, auth_token: SimpleNamespace(calls=calls),
            tunnel_readiness_checker=lambda url: False,
            connection_status_delay=0,
        )
    )
    task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()

    checking = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "approve"},
    ).json()
    calling = wait_for_call(client, task["id"], calls)

    assert checking["stage"] == "connection_starting"
    assert calling["phase"] == "calling"
    assert calls.arguments["to"] == "+12025550199"
    events = [json.loads(line)["event"] for line in (tmp_path / "runtime" / "logs" / "events.jsonl").read_text().splitlines()]
    assert "tunnel.health_check_started" in events
    assert "tunnel.health_check_inconclusive" in events
    assert events.index("tunnel.health_check_inconclusive") < events.index("call.placed")


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

    voice_query = parse_qs(urlparse(calls.arguments["url"]).query)
    status_query = parse_qs(urlparse(calls.arguments["status_callback"]).query)
    assert voice_query["task_id"] == ["task-123"]
    assert voice_query["queue_index"] == ["2"]
    assert status_query["task_id"] == ["task-123"]
    assert status_query["queue_index"] == ["2"]
    assert voice_query["capability"] != status_query["capability"]


def test_approved_agentic_plan_places_the_verified_call(monkeypatch, tmp_path):
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
            tunnel_readiness_checker=lambda url: True,
            connection_status_delay=0,
        )
    )
    task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()

    approved = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "approve"},
    )

    assert approved.status_code == 200
    calling = wait_for_call(client, task["id"], calls)
    assert approved.json()["stage"] == "connection_starting"
    assert calling["phase"] == "calling"
    assert calls.arguments["to"] == "+12025550199"
    assert parse_qs(urlparse(calls.arguments["url"]).query)["task_id"] == [task["id"]]


def test_live_instruction_reports_when_realtime_injection_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(RealtimeSessionHub, "inject", lambda self, task_id, text: async_false())
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
            tunnel_readiness_checker=lambda url: True,
            connection_status_delay=0,
        )
    )
    task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()
    checking = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "approve"},
    ).json()
    calling = wait_for_call(client, task["id"], calls)
    assert checking["stage"] == "connection_starting"
    assert calling["phase"] == "calling"

    response = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "instruction", "value": "The apartment number is 4B."},
    )

    assert response.status_code == 409
    assert "not delivered" in response.json()["detail"]
    unchanged = client.get(f"/api/tasks/{task['id']}").json()
    assert not any(event.get("text") == "The apartment number is 4B." for event in unchanged["events"])
    events = [json.loads(line)["event"] for line in (tmp_path / "runtime" / "logs" / "events.jsonl").read_text().splitlines()]
    assert "realtime.instruction_received" in events
    assert "realtime.instruction_rejected" in events


async def async_false():
    return False


async def async_injection_failure():
    raise RuntimeError("routing failed")


def test_live_instruction_failure_is_nonfatal_and_visible_in_private_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(RealtimeSessionHub, "inject", lambda self, task_id, text: async_injection_failure())
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
            tunnel_readiness_checker=lambda url: True,
            connection_status_delay=0,
        )
    )
    task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()
    client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "approve"},
    )
    wait_for_call(client, task["id"], calls)

    response = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "instruction", "value": "Aug 1"},
    )

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["phase"] == "calling"
    assert snapshot["events"][-2]["speaker"] == "relay_private"
    assert "Please send it again" in snapshot["events"][-2]["text"]
    assert snapshot["events"][-1]["text"] == "Answer not applied · the call remains active"
    event_names = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime" / "logs" / "events.jsonl").read_text().splitlines()
    ]
    assert "realtime.instruction_failed" in event_names
    assert "task.private_call_message_retry_requested" in event_names


def test_terminal_status_callback_revokes_the_call_capabilities(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    calls = FakeCalls()
    capabilities = CallCapabilityStore()
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
            capability_store=capabilities,
            tunnel_readiness_checker=lambda url: True,
            connection_status_delay=0,
        )
    )
    task = client.post("/api/tasks", json={"goal": "Request a service quote."}).json()
    client.post(f"/api/tasks/{task['id']}/actions", json={"action": "answer", "value": "approve"})
    wait_for_call(client, task["id"], calls)
    status_path = urlparse(calls.arguments["status_callback"]).path + "?" + urlparse(
        calls.arguments["status_callback"]
    ).query
    parameters = {
        "AccountSid": "ACtest",
        "CallSid": "CA123",
        "CallStatus": "completed",
    }

    completed = client.post(status_path, data=parameters)
    replay = client.post(status_path, data=parameters)

    assert completed.status_code == 200
    assert replay.status_code == 403
    assert client.get(f"/api/tasks/{task['id']}").json()["stage"] == "execution_failed"


def capability_client(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "runtime"))
    tunnel = TunnelManager(
        8765,
        launcher=lambda port: SimpleNamespace(tunnel="https://relay.trycloudflare.com"),
        terminator=lambda port: None,
    )
    tunnel.acquire()
    capabilities = CallCapabilityStore()
    capability = capabilities.issue("task-123", 0, "ACtest")
    capabilities.bind(capability, "CA123")
    client = TestClient(
        create_app(
            credential_store=configured_store(tmp_path),
            tunnel_manager=tunnel,
            capability_store=capabilities,
        )
    )
    return client, capability, capabilities


def voice_request(capability, extra_query=""):
    query = f"capability={capability.voice_token}&task_id=task-123&queue_index=0{extra_query}"
    return f"/api/twilio/voice?{query}", f"https://relay.trycloudflare.com/api/twilio/voice?{query}"


def webhook_parameters(**overrides):
    return {"AccountSid": "ACtest", "CallSid": "CA123", "From": "+12025550199", **overrides}


def test_voice_accepts_correct_capability_with_missing_or_valid_signature(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    path, url = voice_request(capability)
    parameters = webhook_parameters()

    unsigned = client.post(path, data=parameters)
    signature = RequestValidator("test-auth-token").compute_signature(url, parameters)
    signed = client.post(path, data=parameters, headers={"X-Twilio-Signature": signature})

    assert unsigned.status_code == 200
    assert signed.status_code == 200
    assert f"wss://relay.trycloudflare.com/api/twilio/media/{capability.media_token}" in unsigned.text
    assert 'name="task_id" value="task-123"' in unsigned.text


@pytest.mark.parametrize("query", ["", "capability=wrong&task_id=task-123&queue_index=0"])
def test_voice_rejects_missing_or_wrong_capability(monkeypatch, tmp_path, query):
    client, _, _ = capability_client(monkeypatch, tmp_path)
    response = client.post(f"/api/twilio/voice{f'?{query}' if query else ''}", data=webhook_parameters())

    assert response.status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"AccountSid": "ACother"},
        {"CallSid": "CAother"},
    ],
)
def test_voice_binds_capability_to_account_and_call(monkeypatch, tmp_path, overrides):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    path, _ = voice_request(capability)

    response = client.post(path, data=webhook_parameters(**overrides))

    assert response.status_code == 403


def test_valid_capability_with_invalid_signature_is_rejected_and_token_is_not_logged(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    path, url = voice_request(capability)
    parameters = webhook_parameters()

    invalid = client.post(path, data=parameters, headers={"X-Twilio-Signature": "invalid"})
    assert invalid.status_code == 403

    log_text = (tmp_path / "runtime" / "logs" / "events.jsonl").read_text()
    assert capability.voice_token not in log_text
    assert capability.status_token not in log_text
    assert capability.media_token not in log_text
    records = [json.loads(line) for line in log_text.splitlines()]
    rejected = [record for record in records if record["event"] == "twilio.signature_rejected"]
    invalid_diagnostic = rejected[-1]["payload"]
    validator = RequestValidator("test-auth-token")
    assert invalid_diagnostic == {
        "path": "/api/twilio/voice",
        "parameter_names": ["AccountSid", "CallSid", "From"],
        "external_url": redact_capabilities(url),
        "raw_query_string": redact_capabilities(urlparse(url).query),
        "received_signature": "invalid",
        "url_with_port": redact_capabilities(url.replace(".com/", ".com:443/")),
        "computed_signature_with_port": validator.compute_signature(
            url.replace(".com/", ".com:443/"),
            parameters,
        ),
        "url_without_port": redact_capabilities(url),
        "computed_signature_without_port": validator.compute_signature(url, parameters),
    }


def test_twilio_voice_signature_preserves_repeated_form_values(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    path, url = voice_request(capability)
    parameters = FormData(
        [
            ("AccountSid", "ACtest"),
            ("CallSid", "CA123"),
            ("Repeated", "first"),
            ("Repeated", "second"),
        ]
    )
    signature = RequestValidator("test-auth-token").compute_signature(url, parameters)

    response = client.post(
        path,
        content="AccountSid=ACtest&CallSid=CA123&Repeated=first&Repeated=second",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature,
        },
    )

    assert response.status_code == 200


def test_status_accepts_correct_capability_without_signature_and_rejects_wrong_or_missing(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    parameters = webhook_parameters(CallStatus="ringing")
    valid_path = f"/api/twilio/status?capability={capability.status_token}&task_id=task-123&queue_index=0"

    valid = client.post(valid_path, data=parameters)
    wrong = client.post("/api/twilio/status?capability=wrong", data=parameters)
    missing = client.post("/api/twilio/status", data=parameters)

    assert valid.status_code == 200
    assert wrong.status_code == 403
    assert missing.status_code == 403


def test_status_accepts_correct_capability_and_valid_signature(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    path = f"/api/twilio/status?capability={capability.status_token}&task_id=task-123&queue_index=0"
    url = f"https://relay.trycloudflare.com{path}"
    parameters = webhook_parameters(CallStatus="ringing")
    signature = RequestValidator("test-auth-token").compute_signature(url, parameters)

    response = client.post(path, data=parameters, headers={"X-Twilio-Signature": signature})

    assert response.status_code == 200


def test_twilio_media_websocket_accepts_correct_capability_with_valid_signature(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)
    path = f"/api/twilio/media/{capability.media_token}"
    url = f"wss://relay.trycloudflare.com{path}"
    signature = RequestValidator("test-auth-token").compute_signature(url, {})

    with client.websocket_connect(
        path,
        headers={"X-Twilio-Signature": signature},
    ) as websocket:
        websocket.send_json({"event": "stop"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            websocket.receive_json()

    assert disconnected.value.code == 1008


def test_twilio_media_websocket_accepts_correct_capability_without_signature(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)

    with client.websocket_connect(f"/api/twilio/media/{capability.media_token}") as websocket:
        websocket.send_json({"event": "stop"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            websocket.receive_json()

    assert disconnected.value.code == 1008


@pytest.mark.parametrize("path", ["/api/twilio/media", "/api/twilio/media/wrong"])
def test_twilio_media_websocket_rejects_missing_or_wrong_capability(monkeypatch, tmp_path, path):
    client, _, _ = capability_client(monkeypatch, tmp_path)

    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(path):
            pass

    assert disconnected.value.code == 1008

    records = [
        json.loads(line)
        for line in (tmp_path / "runtime" / "logs" / "events.jsonl").read_text().splitlines()
    ]
    assert [record["event"] for record in records[-2:]] == [
        "media.connection_attempt",
        "media.capability_rejected",
    ]
    assert records[-1]["payload"]["capability_present"] == (path != "/api/twilio/media")
    assert "wrong" not in json.dumps(records[-2:])


def test_twilio_media_websocket_rejects_valid_capability_with_invalid_signature(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)

    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(
            f"/api/twilio/media/{capability.media_token}",
            headers={"X-Twilio-Signature": "invalid"},
        ):
            pass

    assert disconnected.value.code == 1008


def test_call_listener_requires_its_own_active_capability(monkeypatch, tmp_path):
    client, capability, _ = capability_client(monkeypatch, tmp_path)

    with pytest.raises(WebSocketDisconnect) as wrong:
        with client.websocket_connect("/api/twilio/listen/wrong"):
            pass
    with pytest.raises(WebSocketDisconnect) as not_connected:
        with client.websocket_connect(f"/api/twilio/listen/{capability.listen_token}") as websocket:
            websocket.receive_json()

    assert wrong.value.code == 1008
    assert not_connected.value.code == 1013
    assert capability.listen_token != capability.media_token


def test_revoked_call_capabilities_cannot_be_replayed(monkeypatch, tmp_path):
    client, capability, capabilities = capability_client(monkeypatch, tmp_path)
    capabilities.revoke("CA123")
    voice_path, _ = voice_request(capability)

    voice = client.post(voice_path, data=webhook_parameters())
    status = client.post(
        f"/api/twilio/status?capability={capability.status_token}",
        data=webhook_parameters(CallStatus="ringing"),
    )
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(f"/api/twilio/media/{capability.media_token}"):
            pass
    with pytest.raises(WebSocketDisconnect) as listener_disconnected:
        with client.websocket_connect(f"/api/twilio/listen/{capability.listen_token}"):
            pass

    assert voice.status_code == 403
    assert status.status_code == 403
    assert disconnected.value.code == 1008
    assert listener_disconnected.value.code == 1008


def test_capability_access_log_filter_redacts_http_and_media_tokens():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "POST",
            "/api/twilio/voice?capability=voice-secret&task_id=task-123",
            "1.1",
            200,
        ),
        None,
    )
    media_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s",
        ("/api/twilio/media/media-secret",),
        None,
    )
    listener_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s",
        ("/api/twilio/listen/listen-secret",),
        None,
    )
    access_filter = CapabilityAccessLogFilter()

    access_filter.filter(record)
    access_filter.filter(media_record)
    access_filter.filter(listener_record)

    assert "voice-secret" not in record.getMessage()
    assert "media-secret" not in media_record.getMessage()
    assert "[REDACTED]" in record.getMessage()
    assert "[REDACTED]" in media_record.getMessage()
    assert "listen-secret" not in listener_record.getMessage()
    assert "[REDACTED]" in listener_record.getMessage()


def test_uvicorn_access_handler_installs_capability_redaction():
    config = relay_log_config()

    assert config["filters"]["relay_capabilities"]["()"] is CapabilityAccessLogFilter
    assert "relay_capabilities" in config["handlers"]["access"]["filters"]
