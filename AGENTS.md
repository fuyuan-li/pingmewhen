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
- Save structured event logs and transcripts under the ignored repo-local `.relay/` directory by default. Never log secure-mode values, card data, full SSNs, secrets, or auth tokens.
- The demo uses a simulated insurer and fake payment data.

## Implemented now

- The deterministic preview is runnable end to end: validated address/PDF clarification, editable planning, explicit start approval, paced synthetic quote calls with a fresh introduction for each representative, interruptible barge-in, an animated Private Workspace and Call Console, per-call transcript tabs with a vertical history bookmark, factual comparison back in planning, later approval gates, simulated takeover/resume, field-by-field secure payment simulation, and local JSONL logs.
- Takeover does not connect microphone or phone audio yet. UI and documentation must call it simulated until a real media bridge exists.
- Browser speech in the preview plays on the user device; it is not injected into a phone call. Outbound-only local audio requires the shared media bridge.
- It does not yet contain model-driven planning, Realtime voice, telephony, or general task execution. Do not describe those as working.

## Explicit non-goals

- No Piper, voice-model downloads, or cross-platform local TTS in P0.
- No real card or SSN handling.
- No real insurance recommendation, ranking, solicitation, binding, or commission.
- No additional CLI commands beyond `relay` and `relay demo`.
- Do not present Relay as merely a calling agent.
- Do not claim ChatGPT/Codex login authorizes Realtime API calls. The demo backend supplies limited Realtime and telephony access.

## Engineering rules

- Read `docs/PRD.md`, `docs/DESIGN.md`, and `docs/IMPLEMENTATION_PLAN.md` before changing scope or architecture.
- Keep model output structured. The model emits UI schemas; it never emits executable frontend code.
- Treat user messages as private instructions unless the call agent deliberately reformulates them for the representative.
- Disclose that Relay is an AI at the beginning of a call.
- Prefer the smallest end-to-end implementation that advances the demo.
- Add tests for state transitions, redaction, approvals, and simulator behavior.

## Verification

```bash
uv sync --dev
uv run pytest
uv run relay
uv run relay demo
```
