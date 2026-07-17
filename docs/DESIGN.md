# Relay technical design

## Architecture

```text
relay CLI
   |
   v
Local FastAPI service ------ local JSONL events/transcripts
   |                         machine-local SQLite task snapshots
   |                         machine-local owner-only credentials
   |
   +------ localhost dashboard
   |
   +------ task orchestrator
   |          |
   |          +-- context and approvals
   |          +-- call plan and outcomes
   |
   +------ on-demand pycloudflared tunnel ------ Twilio webhooks
   |                                                |
   |                                                +-- bidirectional PCMU Media Stream
   |
   +------ user's OpenAI API account <------ Realtime WebSocket
   +------ user's Twilio account

Secure mode:
dashboard -> macOS on-device TTS or user takeover -> call
cloud AI disconnected; transcript paused
```

The local service owns user state, task state, presentation, approvals, durable logs, and provider credential use. Relay is single-tenant by installation: there is no hosted Relay backend, shared account system, or maintainer credential boundary. Deterministic demo mode uses neither provider.

Standard mode resolves `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` from the environment first, then from an owner-only local credential file. The dashboard presents first-run setup only for missing values. The browser sends setup values only to the localhost service; it never calls OpenAI or Twilio directly.

The HTTPS tunnel is lazy. An explicitly approved call acquisition starts `pycloudflared`, obtains a `trycloudflare.com` URL, and supplies voice and status URLs on that individual Twilio Call creation request. Twilio console webhook configuration and `RELAY_PUBLIC_BASE_URL` are not required. A reference count keeps the tunnel alive for active calls; terminal status callbacks and process shutdown tear it down.

Every inbound Twilio HTTP webhook is validated before handling with the Twilio SDK's `RequestValidator`, the exact public URL, submitted form parameters, the `X-Twilio-Signature` header, and the local user's Auth Token. Missing or invalid signatures receive HTTP 403.

Standard `relay` uses the Responses API with a Pydantic Structured Output schema for private planning. The model may ask for missing context, use hosted web search for current official contacts, and propose typed actions, but application code owns approval transitions and execution permissions. A phone action is executable only with an E.164 number whose source is classified: researched contacts require an official HTTP(S) source URL, while a number directly supplied by the user is trusted as user-sourced and requires no artificial URL. After approval, application code queues those calls, gives each call a new Realtime session, passes Twilio's PCMU audio in both directions, persists completed transcript turns, and advances the queue. `relay demo` selects the deterministic insurance engine instead. Both engines persist complete namespaced task snapshots in SQLite.

## Components

### CLI

- Distribution name: `relay-agent`
- Python import package: `relay_agent`
- Executable: `relay`
- `relay` opens the standard dashboard.
- `relay demo` opens the single demo workflow.

### Local API

FastAPI exposes localhost-only HTTP/WebSocket endpoints for:

- runtime health and configuration
- task creation and state
- live transcript events
- private user instructions
- structured interaction responses
- approvals
- takeover and secure-mode transitions
- local event/history queries

It must bind to `127.0.0.1` by default.

### Dashboard

The production dashboard uses two coordinated surfaces:

1. **Private Workspace:** goal/context intake, clarification dialogue, editable call plan, contact details, and approve/hold/decline controls.
2. **Call Console:** external execution, paced transcript turns, per-call tabs, and intervention controls.

The normal insurance sequence is `planning → quote calls → planning comparison/decision → application callback → planning outcome`. With no call history, the Private Workspace fills the dashboard. During a call, the Call Console gradually widens on the right while the Private Workspace narrows, greys out, and becomes review-only. When calls end, the console collapses in place into a vertical right-edge history bookmark and the Private Workspace gradually reclaims the width. Opening the bookmark reverses that animation and shows read-only call history beside the active Private Workspace.

The Call Console contains one persistent tab per external connection. The three quote calls therefore produce three tabs, and the later application callback produces a fourth—even when it targets the same insurer. Relay retains goal and user context across tabs, while every tab starts a new representative-facing conversation from disclosure and purpose because the representative may be different.

The primary active-call layout is:

- compact call header
- left/right conversation bubbles
- distinct private user bubbles
- optional quick-reply row
- persistent instruction box
- permanent Take Over button

The transcript is the primary live representation. Summaries are produced between calls and at task completion, not continuously on every turn.

Task memory persists across the complete goal, but representative conversation context resets at every new call. Each representative hears a fresh AI disclosure, purpose, and the relevant known facts before Relay requests a quote or resumes an application.

The deterministic simulator keeps future turns in a backend queue. The UI requests one turn at a time. A user barge-in is placed at the front of that queue as a private user message, a contextually reformulated Relay utterance, and a simulated representative response. The pending script then resumes.

### Real takeover media path

The deterministic preview does not connect audio. Its takeover control only pauses the scripted state machine and is labeled accordingly.

A real takeover requires three live participants sharing one media bridge:

```text
representative PSTN/SIP leg ─┐
Relay Realtime audio leg ────┼─ local media bridge
user browser WebRTC leg ─────┘
```

The browser obtains microphone audio with WebRTC. The phone leg enters through a SIP trunk or telephony provider. During Relay mode, the local media bridge publishes Relay audio and keeps the user microphone muted. During takeover, it cancels/pauses Relay output, unmutes the browser track into the same conference, and continues routing representative audio to the browser. Returning control reverses that switch. Ephemeral browser credentials and standard API credentials remain local-backend responsibilities.

OpenAI recommends WebRTC for browser Realtime clients and documents browser microphone tracks and ephemeral credentials: https://developers.openai.com/api/docs/guides/realtime-webrtc. OpenAI also documents SIP phone connectivity and call monitoring: https://developers.openai.com/api/docs/guides/realtime-sip. Combining both legs in a shared conference is Relay architecture, not a capability that the current deterministic app already provides.

For the safe hackathon demo, the representative can be a simulated voice participant rather than a real insurer, but the user and simulated representative should still exchange actual audio through the same conference. That proves takeover without calling a real business.

### Task orchestrator

The orchestrator owns the goal and may schedule zero, one, or several calls. The insurance demo is a task recipe, not a special product mode in the core domain.

The production execution slice supports approved phone-call actions. Approval is durably recorded, and application code refuses to dial non-E.164 targets or researched contacts without an official source URL. User-provided contacts are explicitly marked as such and do not require a URL. Non-phone actions remain planning artifacts and are not executed. This prevents the planning model from turning an unimplemented action into a fake success.

Core state:

```text
Task
  id
  goal
  context references
  status
  planned actions
  active call
  pending interaction
  pending approval
  outcomes
```

### Call state machine

```text
PREPARING
  -> DIALING
  -> CONNECTED
  -> WAITING_FOR_USER (call may continue if question is non-blocking)
  -> APPROVAL_REQUIRED
  -> SECURE_HANDOFF_PENDING
  -> SECURE_LOCAL | HUMAN_TAKEOVER
  -> CONNECTED
  -> COMPLETED | FAILED
```

Only explicit events may change state. Every transition is logged.

### Structured interactions

The voice agent emits data, never frontend code:

```json
{
  "type": "single_choice",
  "call_id": "call_demo_1",
  "question": "Do you operate a business from this address?",
  "why_it_matters": "The representative requires this underwriting fact.",
  "blocking": true,
  "sensitivity": "normal",
  "options": [
    {"value": "no", "label": "No"},
    {"value": "yes", "label": "Yes"},
    {"value": "occasional", "label": "Occasionally"},
    {"value": "unsure", "label": "Not sure"}
  ]
}
```

Schemas are validated before rendering and before their responses reach the call agent.

### Secure local voice

P0 supports macOS built-in speech synthesis only. There are no downloaded or bundled voice models.

Secure-mode invariants:

1. Cloud AI receives no inbound or outbound payment audio.
2. Transcript and call-content logging are paused.
3. Sensitive form values remain in local process memory only.
4. Values are cleared after use.
5. The user can take over at any time.
6. P0 accepts fake values only.

Payment is a field-by-field state machine, not one combined form: the representative asks for one field; Relay yields the outbound channel; local TTS speaks only that field; its completion signal returns the channel to Relay; and the representative may then request the next field. The deterministic browser preview demonstrates these transitions with device audio. The production bridge gates both Realtime directions, generates speech in memory with macOS AVSpeechSynthesizer, converts it to PCMU, publishes it only to the representative leg, and waits for Twilio's playback mark before reconnecting Realtime.

The simulated representative will not intentionally repeat fake card data. In production, a repeated protected-field request keeps the Realtime gate closed and transitions the durable call state to `HUMAN_TAKEOVER` rather than speaking the value again.

## Authentication and model access

Codex is used as Relay's repository-scale engineering agent, guided by `AGENTS.md`, the PRD, this design, and the implementation plan. It implements, reviews, and verifies the cross-cutting product slice. GPT-5.6 has a distinct runtime role: standard `relay` uses it through the Responses API and Pydantic Structured Outputs for private planning, while application code owns approval and execution permissions.

ChatGPT/Codex login authorizes Codex workloads; it cannot be treated as authorization for arbitrary Relay API calls. Standard `relay` therefore uses the local user's OpenAI API key. The key remains in the local backend process during API requests and is never returned by an API response or written to an event log.

Twilio REST calls use Account SID + Auth Token Basic Auth. The same Auth Token is required by Twilio's webhook signature algorithm, so Relay neither requires nor prompts for an API Key SID/Secret.

Credentials entered in first-run setup are written to `~/.relay/credentials.json` by default with mode `0600`; setting `RELAY_DATA_DIR` relocates the file with the rest of Relay's local state. Environment variables take precedence over stored values. There is no per-user encryption or tenant partition because the process and file are owned by one local operating-system user.

Deterministic `relay demo` bypasses provider setup. It remains the zero-credential judge path.

If OpenAI publishes an official third-party ChatGPT authentication and Realtime entitlement flow, it can replace local API-key entry later. Do not implement undocumented token reuse.

## Logging model

Events are append-only JSON objects:

```json
{
  "timestamp": "2026-07-16T18:00:00Z",
  "event": "transcript.turn",
  "task_id": "task_123",
  "call_id": "call_456",
  "payload": {
    "speaker": "representative",
    "visibility": "shared",
    "text": "Do you operate a business from the home?"
  }
}
```

Required event families:

- `runtime.*`
- `task.*`
- `call.*`
- `transcript.*`
- `interaction.*`
- `approval.*`
- `takeover.*`
- `secure_mode.*`
- `tool.*`
- `error.*`
- `latency.*`

The log writer recursively redacts known sensitive keys. Secure mode additionally blocks all content-bearing transcript events.

## Simulator

The simulator is a real conversational endpoint backed by scenario state, not a fixed transcript animation. Three insurer profiles vary rates, questions, and follow-ups. Sanitized observations from disclosed/consented research calls may inform synthetic fixtures, but raw third-party conversations and PII are not shipped.
