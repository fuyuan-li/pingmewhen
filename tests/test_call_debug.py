import json
import stat

from relay_agent.call_debug import CallDebugTrace


def test_call_debug_trace_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RELAY_DEBUG_CALL_CONTEXT", raising=False)

    trace = CallDebugTrace("task-1", "CA1")
    trace.append("speaker.context", {"private": "value"})

    assert trace.path is None
    assert not (tmp_path / "debug").exists()


def test_call_debug_trace_writes_private_context_only_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_DEBUG_CALL_CONTEXT", "1")

    trace = CallDebugTrace("task-1", "CA1")
    trace.append("speaker.context", {"private": "1079 Commonwealth Ave"})

    assert trace.path is not None
    record = json.loads(trace.path.read_text())
    assert record["event"] == "speaker.context"
    assert record["payload"]["private"] == "1079 Commonwealth Ave"
    assert stat.S_IMODE(trace.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(trace.path.parent.stat().st_mode) == 0o700
