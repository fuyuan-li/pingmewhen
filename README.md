# PingMeWhen

> Hand off the call. PingMeWhen makes it for you, keeps you in the loop visually, and pings you in only when your knowledge, approval, or voice is actually needed.

PingMeWhen is a local-first, human-supervised voice agent. Give it a goal and the relevant context; it plans the call, dials in real time, and turns a synchronous phone conversation into a low-attention visual experience. You watch the live transcript, answer with quick replies or text, redirect strategy, approve anything consequential, listen to the audio, or take over and speak — by typing — whenever you want.

The name is the point: the agent works in the background and pings you only when you're needed.

## What it does

- **Real outbound calls on your own keys.** Bring your own Twilio and OpenAI credentials. There is no hosted backend, shared account, or shared credential store — everything runs on your machine.
- **Plan first, approve, then act.** A model-driven planner turns your goal and any attached PDF or text file into a reviewable plan. Nothing is dialed until you approve it.
- **Two surfaces.** The **Call Console** shows the representative's speech and the agent's replies live; the **Private Workspace** is where you steer, answer, and approve — privately, never heard on the call.
- **Asks before it commits.** Prices, scheduling, enrollment, and payments pause and consult you; a budget or preference is context, never permission.
- **Human takeover.** Tap **Take over** and type — your words are spoken to the representative by on-device voice, and the whole interface drops into a private dark mode while you're on the line.
- **Listen in.** Monitor the live call audio in your browser, read-only, whenever you want to hear how it's going.
- **Sensitive data stays local.** When a card number or SSN is requested, the cloud model is muted and you type the value yourself; it's spoken by local voice only — never sent to the model, never logged.

## Install and run

From source with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --dev
uv run pingmewhen          # standard mode (uses your credentials)
uv run pingmewhen demo     # fully simulated demo, no credentials needed
```

To install it as a command-line tool:

```bash
uv tool install .          # then run `pingmewhen` or `pingmewhen demo` from anywhere
```

Standard mode opens a first-run setup screen when any required credential is missing:

- `OPENAI_API_KEY`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`

Environment variables take precedence, so a local `.env` works too. Otherwise the dashboard stores credentials in an owner-only local file. PingMeWhen's maintainers never receive them and pay none of your provider costs.

The dashboard's **Models** control configures three roles independently: planning (`gpt-5.4-mini` default), Realtime voice (`gpt-realtime-2.1-mini` default), and transcription (`gpt-4o-mini-transcribe` default). Choices are stored locally. By default the dashboard runs at `http://127.0.0.1:8765`; set `RELAY_DATA_DIR` to relocate local data or `RELAY_PORT` to change the port.

Production calls start a `pycloudflared` tunnel alongside the local server so Twilio can reach the voice, status, and media webhooks over HTTPS/WSS. Each call gets separate random, call-bound capability tokens that are revoked when the call ends and redacted from logs; a supplied Twilio signature is also validated locally. Audio stays PCMU end to end between Twilio Media Streams and OpenAI Realtime.

## How Codex and OpenAI models are used

PingMeWhen is built with Codex as the repository-scale engineering agent. The repo-level [`AGENTS.md`](AGENTS.md) gives Codex the durable product, safety, architecture, and verification contract; Codex uses that contract together with the PRD and design docs to implement, review, and test changes across the application rather than generating isolated snippets.

The two OpenAI layers have deliberately different jobs:

- **Codex builds and verifies PingMeWhen:** it works across the repository, keeps the implementation aligned with the product constraints, runs the test suite, and records key decisions in the docs.
- **A user-selected GPT model runs PingMeWhen's private planner and live call:** the planner uses the Responses API with Pydantic Structured Outputs to clarify goals and produce typed action plans, and the call uses OpenAI Realtime for live voice. Application code, not model output, owns every approval and execution boundary.

Codex is central to the engineering workflow, but it is not an audio transport or a substitute credential for the Realtime API.

## Important boundaries

- The agent discloses that it is an AI when it speaks, and never gives itself a fake human name.
- You select outcomes and approve consequential actions; a budget or preference never authorizes a decision on its own.
- Sensitive fields (card number, CVV, SSN) are handled locally: the cloud model is removed from the media path, transcription is paused, and the value you type is synthesized to speech on-device and injected only into the representative's call leg. It is never sent to the model or written to logs.
- Human takeover is text-to-speech today (you type; local voice speaks). Live browser-microphone takeover is not connected yet and is not represented as available.
- Attached context (PDF or text) is stored locally. Standard planning sends bounded extracted text to the configured model; the simulated demo does not.
- The `pingmewhen demo` workflow is fully simulated and uses only fake data.

## Repository map

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── docs/
│   ├── DECISIONS.md
│   ├── DESIGN.md
│   ├── IMPLEMENTATION_PLAN.md
│   └── PRD.md
├── src/relay_agent/          # internal package name (the shipped command is `pingmewhen`)
│   ├── app.py
│   ├── cli.py
│   ├── agentic_engine.py
│   ├── planner.py
│   ├── gatekeeper.py
│   ├── realtime_bridge.py
│   ├── telephony.py
│   ├── local_tts.py
│   ├── tunnel.py
│   └── static/
└── tests/
```
