from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from relay_agent.app import create_app
from relay_agent.context_store import ContextStore
from relay_agent.event_log import EventLog


def test_task_api_reaches_comparison(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MODE", "demo")
    client = TestClient(create_app())

    created = client.post("/api/tasks", json={"goal": "Gather three quotes."})
    assert created.status_code == 200
    task = created.json()

    planned = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "instruction", "value": "123 Demo Street"},
    )
    assert planned.status_code == 200
    task = planned.json()
    task = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "approve"},
    ).json()
    while task["auto_advance"]:
        task = client.post(
            f"/api/tasks/{task['id']}/actions",
            json={"action": "advance", "value": ""},
        ).json()
    assert task["stage"] == "claims_history"

    task = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "no"},
    ).json()
    while task["auto_advance"]:
        task = client.post(
            f"/api/tasks/{task['id']}/actions",
            json={"action": "advance", "value": ""},
        ).json()

    assert task["stage"] == "select_insurer"
    assert len(task["quotes"]) == 3


def test_api_rejects_empty_goal(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MODE", "demo")
    client = TestClient(create_app())

    response = client.post("/api/tasks", json={"goal": "   "})

    assert response.status_code == 422
    assert "Describe" in response.json()["detail"]


def test_pdf_context_upload_is_stored_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MODE", "demo")
    client = TestClient(create_app())
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(buffer)

    uploaded = client.post(
        "/api/contexts",
        files={"file": ("sample-policy.pdf", buffer.getvalue(), "application/pdf")},
    )

    assert uploaded.status_code == 200
    metadata = uploaded.json()
    assert metadata["filename"] == "sample-policy.pdf"
    assert metadata["pages"] == 1
    assert (tmp_path / "contexts" / metadata["id"] / "source.pdf").exists()
    assert (tmp_path / "contexts" / metadata["id"] / "extracted.txt").exists()


def test_pdf_address_extractor_finds_street_address(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    contexts = ContextStore(EventLog(tmp_path / "events.jsonl"), tmp_path / "contexts")
    assert contexts._find_address("Premises: 123 Main Street, Washington, DC 20001") == (
        "123 Main Street, Washington, DC 20001"
    )


def test_default_home_root_is_shared_by_logs_state_and_contexts(monkeypatch, tmp_path):
    home = tmp_path / "home"
    unrelated_cwd = tmp_path / "unrelated-project"
    unrelated_cwd.mkdir()
    monkeypatch.delenv("RELAY_DATA_DIR", raising=False)
    monkeypatch.setenv("RELAY_MODE", "demo")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(unrelated_cwd)
    client = TestClient(create_app())
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(buffer)

    runtime = client.get("/api/runtime").json()
    uploaded = client.post(
        "/api/contexts",
        files={"file": ("context.pdf", buffer.getvalue(), "application/pdf")},
    ).json()

    root = home / ".relay"
    assert Path(runtime["event_log"]) == root / "logs" / "events.jsonl"
    assert Path(runtime["state_db"]) == root / "state" / "relay.db"
    assert (root / "contexts" / uploaded["id"] / "source.pdf").exists()
    assert not (unrelated_cwd / ".relay").exists()
