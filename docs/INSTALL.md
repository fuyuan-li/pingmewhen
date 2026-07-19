# Install PingMeWhen

PingMeWhen is currently supported on macOS because protected type-to-speak uses Apple's built-in `AVSpeechSynthesizer` locally.

## One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/fuyuan-li/pingmewhen/main/install.sh | sh
```

The installer:

1. installs `uv` when it is not already available;
2. installs PingMeWhen and its isolated Python environment with `uv tool install`;
3. renders a silent in-memory phrase through the same macOS speech path used during protected calls, confirming that local TTS works.

PingMeWhen does not download a separate voice model. Its production local voice is the speech engine already included with macOS; the Python bridge is installed with the application dependencies.

After installation, run:

```bash
pingmewhen
```

The browser opens the local first-run setup. Prepare your own:

- OpenAI API key;
- Twilio Account SID;
- Twilio Auth Token;
- voice-capable Twilio phone number.

The credentials are stored in an owner-only file on your Mac. PingMeWhen has no hosted account service and its maintainers do not receive your keys.

## Install from a local checkout

```bash
uv tool install --force .
pingmewhen --check-install
pingmewhen
```

For development:

```bash
uv sync --dev
uv run pytest
uv run pingmewhen
```

## Update or uninstall

```bash
uv tool install --force git+https://github.com/fuyuan-li/pingmewhen.git
uv tool uninstall pingmewhen
```
