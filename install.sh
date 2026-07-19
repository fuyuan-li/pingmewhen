#!/bin/sh

set -eu

PACKAGE_SOURCE=${PINGMEWHEN_PACKAGE_SOURCE:-git+https://github.com/fuyuan-li/relay.git}

if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' "PingMeWhen currently requires macOS for protected on-device speech."
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' "PingMeWhen needs curl to install uv. Install curl, then run this command again."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "uv was installed, but it is not available in this shell yet. Open a new terminal and rerun the installer."
  exit 1
fi

printf '%s\n' "Installing PingMeWhen and its isolated Python environment…"
uv tool install --force --python 3.12 "$PACKAGE_SOURCE"

TOOL_BIN_DIR=$(uv tool dir --bin 2>/dev/null || true)
if [ -n "$TOOL_BIN_DIR" ]; then
  export PATH="$TOOL_BIN_DIR:$PATH"
fi
export PATH="$HOME/.local/bin:$PATH"

if ! command -v pingmewhen >/dev/null 2>&1; then
  printf '%s\n' "PingMeWhen installed, but its command directory is not on PATH. Run 'uv tool update-shell', restart your terminal, then run 'pingmewhen'."
  exit 1
fi

printf '%s\n' "Checking macOS on-device speech…"
pingmewhen --check-install

printf '\n%s\n' "PingMeWhen installed successfully. Run: pingmewhen"
printf '%s\n' "Before your first real call, prepare:"
printf '%s\n' "  • your own OpenAI API key"
printf '%s\n' "  • your own Twilio account SID and Auth Token"
printf '%s\n' "  • a voice-capable Twilio phone number"
printf '%s\n' "The first-run dashboard will collect these credentials and store them only on this Mac."
printf '%s\n' "PingMeWhen uses macOS's built-in on-device speech voice, so there is no separate TTS model download."
