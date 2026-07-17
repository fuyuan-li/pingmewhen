# Relay

> Give Relay a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

Relay is a local-first, human-supervised task agent that can use phone calls as an action channel. It converts synchronous voice workflows into a low-attention visual experience: the user can follow a live conversation, answer with quick controls or text, redirect strategy, approve consequential actions, or take over the call.

The OpenAI Build Week demo asks several simulated insurers for renters-insurance quotes, shows a factual comparison, waits for the user to select one, calls back to continue the application, and completes a sandboxed secure-payment flow.

## Current state

The standard `relay` mode has a model-driven private planning loop backed by the OpenAI Responses API and Structured Outputs. It reads the goal and locally extracted PDF text, asks blocking questions, uses hosted web search to resolve current official contact details, produces a structured action plan, and waits for explicit approval. An approved phone action with a sourced E.164 number now dials through Twilio, opens a bidirectional Media Stream to OpenAI Realtime, starts the representative conversation, accepts private live instructions, persists both transcript sides, advances sequential calls, and returns to private review. Complete task state is stored in local SQLite and reloads after restart. Relay is a bring-your-own-key, single-user local tool: it has no hosted backend, Relay account, or shared credential store.

`relay demo` remains the deterministic end-to-end insurance preview. The **Private Workspace** holds task memory; the **Call Console** presents paced simulated calls, barge-in, approval gates, per-call history, and the field-by-field fake payment handoff.

## How Codex and GPT-5.6 are used

Relay is built with Codex as the repository-scale engineering agent. The repo-level [`AGENTS.md`](AGENTS.md) gives Codex the durable product, safety, architecture, and verification contract; Codex uses that contract together with the PRD and design docs to implement, review, and test changes across the application rather than generating isolated snippets. Concrete results include the schema-validated planner, application-owned approval state machine, persistent task store, redacted event log, deterministic call simulator, and their tests.

The two OpenAI layers have deliberately different jobs:

- **Codex builds and verifies Relay:** it works across the repository, keeps implementation aligned with the product constraints, runs the test suite, and records key decisions in the docs.
- **GPT-5.6 runs Relay's private planner:** standard `relay` calls the Responses API with Pydantic Structured Outputs to clarify goals and produce typed action plans; application code, not model output, owns approval and execution boundaries.

Codex is therefore central to the engineering workflow, but it is not an audio transport or a substitute credential for the Realtime API. The submission's Codex Session ID identifies the session in which the core functionality was built.

## Commands

```bash
uv sync --dev
uv run relay
```

Open the single demo mode with:

```bash
uv run relay demo
```

Use `relay demo` to test the complete simulated workflow without credentials. Standard `relay` opens a first-run setup screen when any required credential is missing:

- `OPENAI_API_KEY`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`

Environment variables take precedence, so a local `.env` remains supported. Otherwise the dashboard stores credentials in `~/.relay/credentials.json` with owner-only permissions. Relay's maintainers never receive them and pay none of the user's provider costs.

The three model roles are independently configurable:

```env
# Responses API planning: gpt-5.4-mini, gpt-5.4, or gpt-5.6
OPENAI_MODEL=gpt-5.4-mini

# Realtime speech-to-speech: gpt-realtime-2.1-mini or gpt-realtime-2.1
RELAY_REALTIME_MODEL=gpt-realtime-2.1-mini

# Live input transcription: gpt-4o-mini-transcribe or gpt-4o-transcribe
RELAY_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
```

These are passed through as model IDs rather than restricted to a fixed allowlist, so advanced users can select later compatible models without a Relay release. Text-only GPT-5.4 and GPT-5.6 models cannot be used for `RELAY_REALTIME_MODEL`.

An approved task phone action starts `pycloudflared` on demand, uses its HTTPS address for that call's Twilio voice/status callbacks and WSS media endpoint, and stops the tunnel when no calls remain or Relay exits. There is no `RELAY_PUBLIC_BASE_URL` setup step. Every inbound Twilio HTTP or WebSocket request is checked with Twilio's SDK and the local `TWILIO_AUTH_TOKEN`. Audio remains PCMU end to end between Twilio Media Streams and OpenAI Realtime; the dashboard polls durable local task state to show completed transcript turns.

By default, Relay opens `http://127.0.0.1:8765`, writes redacted events under `~/.relay/logs/`, stores durable task state in `~/.relay/state/relay.db`, and keeps credentials in `~/.relay/credentials.json`. Set `RELAY_DATA_DIR` to place all of these under one chosen local directory, or `RELAY_PORT` to change the local port.

## Repository map

```text
.
├── AGENTS.md
├── README.md
├── idea.md
├── pyproject.toml
├── docs/
│   ├── DECISIONS.md
│   ├── DESIGN.md
│   ├── IMPLEMENTATION_PLAN.md
│   └── PRD.md
├── src/relay_agent/
│   ├── app.py
│   ├── cli.py
│   ├── agentic_engine.py
│   ├── credentials.py
│   ├── event_log.py
│   ├── local_tts.py
│   ├── planner.py
│   ├── realtime_bridge.py
│   ├── task_store.py
│   ├── telephony.py
│   ├── tunnel.py
│   └── static/
└── tests/
```

## Important boundaries

- Relay discloses that it is an AI voice assistant.
- The insurance demo presents factual quote information; Relay does not recommend or rank policies.
- The user selects the insurer and approves consequential actions.
- Secure mode removes the cloud AI from the media path and pauses transcription. The fake payment demo requests and speaks card number, expiration, and CVV separately, returning control to Relay between fields.
- In deterministic demo mode, browser TTS plays on the user device. During a production call, protected fake card/SSN fields are synthesized in memory with macOS speech, converted to PCMU, and injected only into the representative’s Twilio leg while both Realtime directions and transcript persistence are gated.
- Real browser microphone takeover is not connected yet. The production Call Console must not be represented as supporting live takeover until that media leg exists.
- Only fake card and identity data are used in the demo.
- PDF context is stored locally under `~/.relay/contexts/`. Standard production planning sends bounded extracted text to the configured model; deterministic demo mode does not.
- ChatGPT/Codex authentication is used only for Codex workloads; Relay does not reuse it as a third-party API credential. Standard mode uses the local user's OpenAI and Twilio credentials, while deterministic demo mode needs neither.
