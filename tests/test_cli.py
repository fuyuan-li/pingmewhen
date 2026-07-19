import sys

import pytest

import relay_agent.cli as cli


def test_install_check_uses_the_local_speech_renderer(monkeypatch, capsys):
    rendered = []

    class Renderer:
        def render_text(self, text):
            rendered.append(text)
            return ["pcmu"]

    monkeypatch.setattr(cli, "MacOSLocalTTS", Renderer)
    monkeypatch.setattr(sys, "argv", ["pingmewhen", "--check-install"])

    cli.main()

    assert rendered == ["PingMeWhen installation check"]
    assert "on-device speech is ready" in capsys.readouterr().out


def test_demo_subcommand_is_not_available(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pingmewhen", "demo"])

    with pytest.raises(SystemExit, match="2"):
        cli.parse_args()
