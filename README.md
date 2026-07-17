# Relay

> Give Relay a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

Relay is a local-first, human-supervised task agent that can use phone calls as an action channel. It converts synchronous voice workflows into a low-attention visual experience: the user can follow a live conversation, answer with quick controls or text, redirect strategy, approve consequential actions, or take over the call.

The OpenAI Build Week demo asks several simulated insurers for renters-insurance quotes, shows a factual comparison, waits for the user to select one, calls back to continue the application, and completes a sandboxed secure-payment flow.

## Current state

The standard `relay` mode now has a model-driven private planning loop backed by the OpenAI Responses API and Structured Outputs. It reads the goal and locally extracted PDF text, asks blocking questions, produces a structured action plan, and waits for explicit approval. Complete task state is stored in local SQLite and reloads after restart. Relay is a bring-your-own-key, single-user local tool: it has no hosted backend, Relay account, or shared credential store. The Twilio control plane can create an explicitly approved outbound call through a lazy local tunnel, but the task engine, Realtime audio, and microphone takeover are not connected to that call path yet.

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

Environment variables take precedence, so a local `.env` remains supported. Otherwise the dashboard stores the values in `~/.relay/credentials.json` with owner-only permissions. Relay's maintainers never receive them and pay none of the user's provider costs. `OPENAI_MODEL` can override the default `gpt-5.6` planner model.

The explicitly approved telephony endpoint starts `pycloudflared` on demand, uses its HTTPS address for that call's Twilio `Url` and status callback, and stops the tunnel when no calls remain or Relay exits. There is no `RELAY_PUBLIC_BASE_URL` setup step. Every inbound Twilio webhook is checked with Twilio's SDK and the local `TWILIO_AUTH_TOKEN`. The task engine still needs to be connected to this endpoint before normal plans can dial.

By default, Relay opens `http://127.0.0.1:8765`, writes redacted events under `.relay/logs/`, and stores durable task state in `.relay/state/relay.db`. Credentials default to the machine-local `~/.relay/credentials.json`. Set `RELAY_DATA_DIR` to place all of these under one chosen local directory, or `RELAY_PORT` to change the local port.

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
│   ├── planner.py
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
- Browser TTS currently plays on the user device. Injecting local TTS only into the representative’s phone leg requires the planned shared media gateway.
- Only fake card and identity data are used in the demo.
- PDF context is stored locally under `.relay/contexts/`. Standard production planning sends bounded extracted text to the configured model; deterministic demo mode does not.
- ChatGPT/Codex authentication is used only for Codex workloads; Relay does not reuse it as a third-party API credential. Standard mode uses the local user's OpenAI and Twilio credentials, while deterministic demo mode needs neither.
