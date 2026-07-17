# Relay

> Give Relay a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

Relay is a local-first, human-supervised task agent that can use phone calls as an action channel. It converts synchronous voice workflows into a low-attention visual experience: the user can follow a live conversation, answer with quick controls or text, redirect strategy, approve consequential actions, or take over the call.

The OpenAI Build Week demo asks several simulated insurers for renters-insurance quotes, shows a factual comparison, waits for the user to select one, calls back to continue the application, and completes a sandboxed secure-payment flow.

## Current state

The standard `relay` mode now has a model-driven private planning loop backed by the OpenAI Responses API and Structured Outputs. It reads the goal and locally extracted PDF text, asks blocking questions, produces a structured action plan, and waits for explicit approval. Complete task state is stored in repo-local SQLite and reloads after restart. Approved external actions stop at an honest connector boundary: real calls, Realtime audio, and microphone takeover are not connected yet.

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

Use `relay demo` to test the complete simulated workflow without credentials. Standard `relay` uses a server-side OpenAI API credential for planning:

```bash
OPENAI_API_KEY=... uv run relay
```

This is an operator/development credential, not a user-facing login design. The planned hosted demo gateway will own it for judges and end users. `OPENAI_MODEL` can override the default `gpt-5.6` planner model.

By default, Relay opens `http://127.0.0.1:8765`, writes redacted events under `.relay/logs/`, and stores durable task state in `.relay/state/relay.db`. Set `RELAY_DATA_DIR` or `RELAY_PORT` to override those defaults.

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
│   ├── event_log.py
│   ├── planner.py
│   ├── task_store.py
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
- ChatGPT/Codex authentication is used only for Codex workloads; Relay does not reuse it as a third-party Realtime API credential. The hosted demo backend owns any Realtime and telephony credentials. This media/authentication boundary is separate from the Codex engineering workflow described above.
