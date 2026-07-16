# Relay

> Give Relay a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

Relay is a local-first, human-supervised task agent that can use phone calls as an action channel. It converts synchronous voice workflows into a low-attention visual experience: the user can follow a live conversation, answer with quick controls or text, redirect strategy, approve consequential actions, or take over the call.

The OpenAI Build Week demo asks several simulated insurers for renters-insurance quotes, shows a factual comparison, waits for the user to select one, calls back to continue the application, and completes a sandboxed secure-payment flow.

## Current state

This repository contains a testable deterministic product preview. It maintains separate planning and external-call panels, moves back to planning for comparison and selection, paces simulated call turns one at a time, supports user barge-in, and gates consequential steps on approval. It does not yet place real calls, connect microphone audio, or use an AI model for planning or conversation; takeover is labeled as a simulation.

## Commands

```bash
uv sync --dev
uv run relay
```

Open the single demo mode with:

```bash
uv run relay demo
```

Use `relay demo` to test the current end-to-end simulated workflow. The normal `relay` command currently exposes the same deterministic preview under a local-mode label; general agentic task execution is not implemented yet.

By default, Relay opens `http://127.0.0.1:8765` and writes redacted structured events under `~/.relay/logs/`. Set `RELAY_DATA_DIR` or `RELAY_PORT` to override those defaults.

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
│   ├── event_log.py
│   └── static/
└── tests/
```

## Important boundaries

- Relay discloses that it is an AI voice assistant.
- The insurance demo presents factual quote information; Relay does not recommend or rank policies.
- The user selects the insurer and approves consequential actions.
- Secure mode removes the cloud AI from the media path and pauses transcription.
- Only fake card and identity data are used in the demo.
- PDF context is stored locally under `~/.relay/contexts/`; its contents are not sent to a model in this deterministic build.
- ChatGPT/Codex authentication does not currently authorize third-party Realtime API use. The hackathon demo uses a limited hosted backend for Realtime and telephony.
