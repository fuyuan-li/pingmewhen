# PingMeWhen agent guidance

## Product

PingMeWhen is a local-first, human-supervised task agent that can use real phone calls as an action channel.

> Give PingMeWhen a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

There is one real product path. `pingmewhen` starts the localhost dashboard; the legacy `relay` executable remains an alias. There is no deterministic demo mode or simulated insurer workflow.

## Product boundaries

- The user supplies their own OpenAI API key and Twilio Account SID, Auth Token, and voice-capable phone number.
- There is no hosted backend, shared account system, multi-tenant state, or maintainer-funded provider usage.
- The dashboard accepts a goal and PDF context, supports private plan revision, and starts no call without explicit approval.
- Speaker is the only live audio model. Gatekeeper checks representative turns and private messages for missing facts, sensitive requests, offers, approvals, and decisions that must return to the user.
- Budgets, preferences, and goals constrain behavior but never authorize a consequential decision.
- Private user text never enters Speaker verbatim. The backend sends only confirmed, typed context updates.
- Calls disclose that PingMeWhen is an AI speaking on behalf of the named user.
- The Call Console shows live transcripts, supports receive-only browser monitoring, and provides keyboard type-to-speak takeover. No browser microphone is connected.
- Protected payment, SSN, and date-of-birth exchanges gate both cloud audio directions and content logging. The value is rendered through macOS on-device speech and sent only to Twilio.
- Store state, credentials, logs, transcripts, and optional debug traces under `~/.relay/` by default. `RELAY_DATA_DIR` may override the root.

## Implementation

- `agentic_engine.py` owns the durable task and approval state machine.
- `planner.py` uses Responses API Structured Outputs for private planning and sourced contact research.
- `realtime_bridge.py` connects Twilio PCMU Media Streams to OpenAI Realtime and owns Speaker gating, takeover, secure mode, and the read-only listener tap.
- `gatekeeper.py` performs text-only authority routing and private-message classification.
- `local_tts.py` uses macOS `AVSpeechSynthesizer`; there are no downloaded or bundled voice models.
- `task_store.py`, `context_store.py`, `credentials.py`, and `event_log.py` use single-user machine-local storage.
- Standard startup creates a background `pycloudflared` tunnel. Before dialing, PingMeWhen reports a public health probe but continues on an inconclusive result.
- Each call uses separate revocable voice, status, media, and browser-listen capabilities. Never log or expose them in task state.

## Engineering rules

- Read `docs/PRD.md`, `docs/DESIGN.md`, and `docs/IMPLEMENTATION_PLAN.md` before changing scope or architecture.
- Keep model output structured. Models emit data schemas, never executable frontend code.
- Treat redaction in `event_log.py` and secure-mode transcript suppression as security boundaries.
- Never log card data, CVVs, full SSNs, passwords, OpenAI API keys, Twilio Auth Tokens, or capability tokens.
- Use Twilio's official signature validator whenever a signature is present; reject an invalid signature.
- Keep the browser listener receive-only and best-effort. It must never backpressure the phone path or receive protected audio.
- Add tests for state transitions, approval boundaries, redaction, telephony authentication, and failure recovery.
- Do not claim ChatGPT or Codex login authorizes PingMeWhen API calls.

## Verification

```bash
uv sync --dev
uv run pytest
uv run pingmewhen --check-install
uv run pingmewhen
```
