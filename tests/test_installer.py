import os
import subprocess
from pathlib import Path


def executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def test_installer_uses_uv_checks_local_tts_and_explains_credentials(tmp_path):
    root = Path(__file__).parents[1]
    script = root / "install.sh"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    command_log = tmp_path / "commands.log"
    executable(binary_dir / "uname", "printf 'Darwin\\n'")
    executable(
        binary_dir / "uv",
        'printf \'uv %s\\n\' "$*" >> "$PINGMEWHEN_TEST_LOG"\n'
        'if [ "$1 $2 $3" = "tool dir --bin" ]; then printf \'%s\\n\' "$(dirname "$0")"; fi',
    )
    executable(
        binary_dir / "pingmewhen",
        'printf \'pingmewhen %s\\n\' "$*" >> "$PINGMEWHEN_TEST_LOG"\n'
        'printf \'PingMeWhen is installed and macOS on-device speech is ready.\\n\'',
    )
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:/usr/bin:/bin",
        "PINGMEWHEN_TEST_LOG": str(command_log),
        "PINGMEWHEN_PACKAGE_SOURCE": "local-test-package",
        "HOME": str(tmp_path / "home"),
    }

    result = subprocess.run(
        ["/bin/sh", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0
    assert "PingMeWhen installed successfully" in result.stdout
    assert "OpenAI API key" in result.stdout
    assert "Twilio account SID and Auth Token" in result.stdout
    assert "no separate TTS model download" in result.stdout
    assert "uv tool install --force --python 3.12 local-test-package" in command_log.read_text()
    assert "pingmewhen --check-install" in command_log.read_text()


def test_installer_contains_the_official_uv_bootstrap():
    source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "command -v uv" in source
    assert "https://astral.sh/uv/install.sh" in source
    assert "uv tool install" in source
