# Relay technical design

## Architecture

```text
relay CLI
   |
   v
Local FastAPI service ------ local JSONL events/transcripts
   |
   +------ localhost dashboard
   |
   +------ task orchestrator
   |          |
   |          +-- context and approvals
   |          +-- call plan and outcomes
   |
   +------ limited demo gateway
              |
              +-- OpenAI Realtime voice session
              +-- telephony / simulated insurer

Secure mode:
dashboard -> macOS on-device TTS or user takeover -> call
cloud AI disconnected; transcript paused
```

The local service owns user state, task state, presentation, approvals, and durable logs. The hosted demo gateway protects hackathon credentials and restricts usage to the simulated workflow.

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

The production dashboard is split across two persistent surfaces:

1. **Private planning:** goal/context intake, clarification dialogue, editable call plan, contact details, and approve/hold/decline controls.
2. **Live call monitor:** entered only for external execution, with paced transcript turns and controls for intervention.

The normal insurance sequence is `planning → quote calls → planning comparison/decision → application callback`. Each panel retains its own history, and the active panel changes automatically; the user may inspect the inactive history panel without sending input to it.

The primary active-call layout is:

- compact call header
- left/right conversation bubbles
- distinct private user bubbles
- optional quick-reply row
- persistent instruction box
- permanent Take Over button

The transcript is the primary live representation. Summaries are produced between calls and at task completion, not continuously on every turn.

The deterministic simulator keeps future turns in a backend queue. The UI requests one turn at a time. A user barge-in is placed at the front of that queue as a private user message, a contextually reformulated Relay utterance, and a simulated representative response. The pending script then resumes.

### Real takeover media path

The deterministic preview does not connect audio. Its takeover control only pauses the scripted state machine and is labeled accordingly.

A real takeover requires three live participants sharing one media bridge:

```text
representative PSTN/SIP leg ─┐
Relay Realtime audio leg ────┼─ media conference / gateway
user browser WebRTC leg ─────┘
```

The browser obtains microphone audio with WebRTC. The phone leg enters through a SIP trunk or telephony provider. During Relay mode, the gateway publishes Relay audio and keeps the user microphone muted. During takeover, it cancels/pauses Relay output, unmutes the browser track into the same conference, and continues routing representative audio to the browser. Returning control reverses that switch. Ephemeral browser credentials and standard API credentials must remain server-side responsibilities.

OpenAI recommends WebRTC for browser Realtime clients and documents browser microphone tracks and ephemeral credentials: https://developers.openai.com/api/docs/guides/realtime-webrtc. OpenAI also documents SIP phone connectivity and call monitoring: https://developers.openai.com/api/docs/guides/realtime-sip. Combining both legs in a shared conference is Relay architecture, not a capability that the current deterministic app already provides.

For the safe hackathon demo, the representative can be a simulated voice participant rather than a real insurer, but the user and simulated representative should still exchange actual audio through the same conference. That proves takeover without calling a real business.

### Task orchestrator

The orchestrator owns the goal and may schedule zero, one, or several calls. The insurance demo is a task recipe, not a special product mode in the core domain.

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

The simulated representative will not intentionally repeat fake card data. A repeat or unexpected verification request routes to human takeover.

## Authentication and model access

ChatGPT/Codex login cannot be treated as authorization for arbitrary OpenAI Realtime API calls. P0 therefore does not request an OpenAI API key from demo users and does not route the voice loop through Codex.

The limited demo gateway owns the OpenAI and telephony credentials, issues only short-lived/restricted access, permits only the simulated workflow, and enforces a small call-minute quota.

If OpenAI publishes an official third-party ChatGPT authentication and Realtime entitlement flow, it can replace the gateway later. Do not implement undocumented token reuse.

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
