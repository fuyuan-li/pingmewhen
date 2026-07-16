from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from relay_agent.app import create_app


def test_task_api_reaches_comparison(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    created = client.post("/api/tasks", json={"goal": "Gather three quotes."})
    assert created.status_code == 200
    task = created.json()

    advanced = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={"action": "answer", "value": "no"},
    )
    assert advanced.status_code == 200
    assert advanced.json()["stage"] == "select_insurer"
    assert len(advanced.json()["quotes"]) == 3


def test_api_rejects_empty_goal(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    client = TestClient(create_app())

    response = client.post("/api/tasks", json={"goal": "   "})

    assert response.status_code == 422
    assert "Describe" in response.json()["detail"]


def test_pdf_context_upload_is_stored_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
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
