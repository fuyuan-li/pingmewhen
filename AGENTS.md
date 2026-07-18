# Relay agent guidance

## Product

Relay is a local-first, human-supervised task agent that can use phone calls as an action channel.

Canonical description:

> Give Relay a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

The renters-insurance workflow is the hackathon demo, not the product boundary.

## P0 scope

- `relay` starts the local backend and opens the localhost dashboard.
- `relay demo` runs the single simulated renters-insurance workflow.
- The dashboard accepts a goal and supporting context.
- Calls appear as a chat: representative speech on the left, Relay speech on the right, and private user-to-Relay instructions in a distinct right-side style.
- A persistent text box lets the user steer Relay at any time.
- Relay renders structured quick replies using prebuilt components.
- Consequential actions require explicit approval.
- A permanent Take Over control lets the user join the call.
- Secure mode mutes/disconnects the cloud AI and pauses transcription. Each requested payment field gets its own Relay → local TTS → Relay cycle; the user can take over instead.
- Save structured event logs and transcripts under the machine-local `~/.relay/` directory by default. `RELAY_DATA_DIR` may override this root. Never log secure-mode values, card data, full SSNs, secrets, or auth tokens.
- The demo uses a simulated insurer and fake payment data.

## Implemented now

- Standard `relay` uses an OpenAI Responses API planner with Pydantic Structured Outputs. It can clarify a general goal, incorporate extracted PDF text, render a structured action plan, and enforce application-owned approve/hold/decline boundaries.
- Standard `relay` is a bring-your-own-key local process. First-run dashboard setup collects the user's `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` only when they are absent from the environment, then saves them in an owner-only machine-local file. ChatGPT login does not supply API access.
- There is no hosted Relay backend, shared account system, or multi-tenant state. Every install uses only that user's credentials, SQLite data, event logs, and session-long local tunnel.
- Approved production plans can execute phone-call actions only when the planner has collected the caller's preferred display name and supplies a valid E.164 number, concrete known facts, and honest contact provenance; researched contacts require a source URL, while user-provided contacts do not. Speaker is the only live audio model and sees representative audio plus approved context. A separate low-latency text Gatekeeper applies a generic user-authority rule to every nontrivial representative turn and routes every private dashboard message. It returns only `continue` or `consult_user`; any missing fact, preference, judgment, permission, correction, commitment, consequential choice, or uncertainty consults the user. A second veto-only authority check reviews every proposed continuation, and either classifier failing causes consultation. The backend alone creates Speaker responses. Budgets, preferences, and goals constrain behavior but never authorize decisions. Consultations receive durable interaction IDs that must be explicitly resolved before Speaker receives a typed decision update. Exact short acknowledgements bypass model classification deterministically. Raw private text never enters Speaker's conversation: the backend appends only a typed, confirmed context item, waits for its conversation-item acknowledgment, and then permits `response.create`; it never replaces the static call instructions mid-call. A pending question remains active until the matching Speaker response completes. Routing or delivery failures stay nonfatal, preserve the active call and prompt, and ask the user to retry. Private/meta messages receive a reply only in the Private Workspace. While `WAITING_FOR_USER`, representative audio continues to be transcribed; normal Speaker output pauses, but the backend may trigger a constrained one-line keep-alive at intervals. Calls return to the Private Workspace after the queue finishes.
- Standard Relay starts one background `pycloudflared` tunnel at application startup and keeps that session lease until shutdown. After plan approval and before each call attempt, the Private Workspace shows a real public `/api/health` reachability check, logs whether it succeeded or was inconclusive, and then attempts the approved Twilio call in either case. Health confirmation is a reachability result, not a claim of end-to-end encryption. Per-call webhook URLs stream bidirectional PCMU audio through Twilio Media Streams and OpenAI Realtime. Every call gets separate high-entropy voice, status, and media capability tokens bound to its approved task, Twilio Account SID, and Call SID. A missing Twilio signature is tolerated only with the correct scoped capability; a present invalid signature is rejected. Capabilities are revoked at terminal call status and never logged.
- Production secure local voice detects card, expiration, CVV, and full-SSN requests; gates both Realtime audio directions; suppresses transcript content; synthesizes one fake field at a time with macOS AVSpeechSynthesizer; injects PCMU only into Twilio; and resumes Realtime only after Twilio confirms playback. A repeated protected-field request enters `HUMAN_TAKEOVER`; the user can explicitly return the still-connected call to Relay, which reopens the existing Realtime audio path with a fresh continuation instruction.
- Full task snapshots persist in machine-local SQLite at `~/.relay/state/relay.db` and reload after restart. Redacted append-only events remain in `~/.relay/logs/`.
- Setting `RELAY_DEBUG_CALL_CONTEXT=1` writes private per-call Speaker and Gatekeeper payloads under `~/.relay/debug/calls/` (or `RELAY_DATA_DIR`) with owner-only permissions. This debug trace is off by default and is separate from the redacted event log.
- The deterministic preview is runnable end to end: validated address/PDF clarification, editable planning, explicit start approval, paced synthetic quote calls with a fresh introduction for each representative, interruptible barge-in, an animated Private Workspace and Call Console, per-call transcript tabs with a vertical history bookmark, factual comparison back in planning, later approval gates, simulated takeover/resume, field-by-field secure payment simulation, and local JSONL logs.
- Takeover does not connect microphone or phone audio yet. UI and documentation must call it simulated until a real media bridge exists.
- Browser speech in the deterministic preview plays on the user device. Production secure speech is generated by the localhost macOS backend and injected only into the Twilio call leg.
- Production execution currently supports approved outbound phone actions and fake-data secure local voice. Browser microphone takeover, automatic structured extraction/comparison, retries, non-phone tools, and real sensitive data do not work yet. Do not imply otherwise.

## Explicit non-goals

- No Piper, voice-model downloads, or cross-platform local TTS in P0.
- No real card or SSN handling.
- No real insurance recommendation, ranking, solicitation, binding, or commission.
- No additional CLI commands beyond `relay` and `relay demo`.
- No hosted multi-tenant service, user accounts, shared credential vault, tenant partitioning, or maintainer-funded API usage.
- Do not present Relay as merely a calling agent.
- Do not claim ChatGPT/Codex login authorizes Relay API calls. Standard mode uses local BYOK credentials; deterministic demo mode uses no provider credentials.

## Engineering rules

- Read `docs/PRD.md`, `docs/DESIGN.md`, and `docs/IMPLEMENTATION_PLAN.md` before changing scope or architecture.
- Keep model output structured. The model emits UI schemas; it never emits executable frontend code.
- Treat user messages as private instructions unless the call agent deliberately reformulates them for the representative.
- Disclose that Relay is an AI at the beginning of a call.
- Prefer the smallest end-to-end implementation that advances the demo.
- Add tests for state transitions, redaction, approvals, and simulator behavior.
- Never log or return OpenAI API keys, Twilio Auth Tokens, or call capability tokens. Require a scoped per-call capability on every Twilio endpoint. Validate a Twilio signature with the official SDK helper whenever the header is present, and reject it if invalid.

## Verification

```bash
uv sync --dev
uv run pytest
uv run relay
uv run relay demo
```
