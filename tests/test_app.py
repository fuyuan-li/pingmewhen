from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from relay_agent.app import create_app
from relay_agent.context_store import ContextStore
from relay_agent.event_log import EventLog


def configure_credentials(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12025550123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_pdf_context_upload_is_stored_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    configure_credentials(monkeypatch)
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
    configure_credentials(monkeypatch)
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


def test_dashboard_keeps_private_speakers_left_and_restores_failed_text():
    source = (Path(__file__).parents[1] / "src" / "relay_agent" / "static" / "index.html").read_text()

    assert "event.speaker !== 'representative' && event.speaker !== 'relay_private'" in source
    assert "if (!planningInstruction.value) planningInstruction.value = optimistic.text;" in source
    assert "Checking secure call tunnel reachability…" in source
    assert "appendConnectionProgress()" in source
    assert "Take over &amp; speak by typing" in source
    assert "/takeover-say" in source
    assert "task?.takeover_active" in source
    assert "takeover-warning" in source
    assert "takeover-active" in source
    assert "Private local voice" in source
    assert "callSummaryMarkup" in source
    assert "configureComposer" in source
    assert "renderTakeoverFieldInput" in source
    assert "Enter the protected value in the field above" in source
    assert "optimisticStart" in source
    assert 'id="listen"' in source
    assert "/listen-capability" in source
    assert "decodeMuLaw" in source
    assert "Know when to bring you in" in source
    assert "missing personal facts, sensitive requests, offers, approvals, or decisions" in source
    assert "four models for four different jobs" in source
    assert "previewMode" not in source
    assert "runtime.mode === 'demo'" not in source
    assert "playMuLawFrame" in source
    assert ".send(" not in source[source.index("async function startListening"):source.index("function stopListening")]
    assert "/secure-fields" not in source


def test_developer_previews_are_separate_from_the_production_dashboard(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    configure_credentials(monkeypatch)
    client = TestClient(create_app())

    gallery = client.get("/previews")
    takeover = client.get("/previews/takeover")
    onboarding = client.get("/previews/onboarding")

    assert gallery.status_code == 200
    assert '/previews/takeover' in gallery.text
    assert '/previews/onboarding' in gallery.text
    assert takeover.status_code == 200
    assert 'Takeover mode preview' in takeover.text
    assert 'Nothing was sent or saved.' in takeover.text
    assert onboarding.status_code == 200
    assert 'CredentialStore onboarding preview' in onboarding.text
    assert 'This preview never submits or stores what you type.' in onboarding.text
    assert '/previews/' not in client.get("/").text
