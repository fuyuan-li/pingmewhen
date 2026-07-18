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
   +------ session-long pycloudflared tunnel --- Twilio webhooks
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

The production HTTPS tunnel is session-long. Standard Relay starts `pycloudflared` in the background with the localhost dashboard, obtains one `trycloudflare.com` URL, and retains one lease until shutdown. Before each approved call attempt, Relay shows `Checking secure call tunnel reachability…` and probes the public `/api/health` endpoint once. It records and displays either confirmed reachability or an inconclusive result, then attempts the Twilio call regardless so a health-check false negative cannot block dialing. The UI next reports that it is calling through the secure call tunnel. Health confirmation proves callback reachability only; it is not described as dedicated or end-to-end encrypted. Active calls hold additional leases released by terminal callbacks. Twilio console webhook configuration, a user-supplied fixed URL, and `RELAY_PUBLIC_BASE_URL` are not required.

Every approved call receives separate random capability tokens for its voice, status, and media endpoints. Voice and status capabilities travel in callback query strings; the media capability travels in the WebSocket path because Twilio Media Streams do not support query strings. Relay binds each capability to the approved task, queue action, Twilio Account SID, and Call SID, and revokes all three at terminal call status. Missing or incorrect capabilities are rejected. When `X-Twilio-Signature` is present, Relay also validates it with the Twilio SDK, exact public URL, submitted form parameters, and local Auth Token; an invalid provided signature is rejected. A missing signature is accepted only when the scoped capability and call identity match. Capability tokens are redacted from event and server access logs.

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

The normal insurance sequence is `planning → quote calls → planning comparison/decision → application callback → planning outcome`. With no call history, the Private Workspace fills the dashboard. During a call, the Call Console gradually widens on the right while the Private Workspace narrows but remains active. All user/Gatekeeper messages, missing-fact prompts, and private replies stay in that workspace. The Call Console is external-only and has no private message composer. When calls end, the console collapses in place into a vertical right-edge history bookmark and the Private Workspace gradually reclaims the width. Opening the bookmark reverses that animation and shows read-only call history beside the active Private Workspace.

The Call Console contains one persistent tab per external connection. The three quote calls therefore produce three tabs, and the later application callback produces a fourth—even when it targets the same insurer. Relay retains goal and user context across tabs, while every tab starts a new representative-facing conversation from disclosure and purpose because the representative may be different.

The primary active-call layout is:

- compact call header
- left/right conversation bubbles
- a parallel interactive Private Workspace with distinct user/Gatekeeper bubbles
- optional quick-reply row
- persistent private instruction box in the Private Workspace
- permanent Take Over button

The transcript is the primary live representation. Summaries are produced between calls and at task completion, not continuously on every turn.

Task memory persists across the complete goal, but representative conversation context resets at every new call. Each representative hears a fresh AI disclosure, purpose, and the relevant known facts before Relay requests a quote or resumes an application.

Production call-time reasoning is split between two roles with the backend as control plane and shared-state owner. Speaker is the sole Realtime audio model and the only model Twilio hears. It receives representative audio, approved original context, and typed confirmed context updates; it never receives raw dashboard messages. Gatekeeper is a text-only component that applies a generic user-authority rule to completed representative transcripts and routes private workspace messages. A conservative deterministic filter treats exact short acknowledgements as safe without a model call. Every other turn first receives a `continue` or `consult_user` classification. Any new user-owned fact, preference, judgment, permission, correction, commitment, consequential choice, or uncertainty consults the user. Every proposed continuation then passes through a separate veto-only authority classifier; either classifier failing or returning malformed output fails closed to consultation. Gatekeeper receives only the approved call purpose, target, concrete known facts, relevant document context, and confirmed updates—not raw private planning history. Server VAD remains enabled with automatic response creation disabled, so only the backend can send `response.create`. A budget, preference, or goal constrains behavior but never authorizes a decision. Each consultation receives a durable interaction ID, faithful representative update, reason, and private question. The interaction remains pending until the user explicitly resolves it and the matching typed context item and Speaker response complete. Consultations produce one brief hold line and transition the task to `WAITING_FOR_USER` while representative audio remains transcribed; constrained keep-alives may play periodically. Routing or delivery failures do not terminate the call: Relay preserves the prompt and requests a retry. Static Speaker identity, role, goal, and boundaries are sent once when Realtime connects. The separately requested first response is an ordinary interruptible turn containing the concise disclosure; the static instructions contain no opening-completion obligation, so barge-in never causes a restart. Response request, creation, and completion diagnostics carry non-content-bearing purpose and correlation IDs. Speaker has no private-input tool.

The deterministic simulator keeps future turns in a backend queue. The UI requests one turn at a time. A user barge-in is placed at the front of that queue as a private user message, a contextually reformulated Relay utterance, and a simulated representative response. The pending script then resumes.

### Typed takeover media path

Production P0 takeover is type-to-speak, not browser microphone audio. The localhost backend cancels and gates Speaker output, pauses Gatekeeper decisions, synthesizes the user's text with macOS AVSpeechSynthesizer, converts it to PCMU, and publishes it directly into the existing Twilio Media Stream. The typed text is not submitted to Speaker as a conversation message.

During ordinary typed takeover, representative audio continues to reach Realtime transcription so the user can read the Call Console, but the backend never requests a Speaker response. On handback, Relay appends one sanitized backend-confirmed continuity item and explicitly resumes Speaker without another introduction. During protected takeover, both Realtime audio directions and content logging remain gated; handback supplies only a content-free marker that the protected exchange finished.

The deterministic preview does not connect phone audio. Browser microphone/conference takeover remains a future extension and is not part of P0.

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
  -> WAITING_FOR_USER (representative input/transcription continues; normal Speaker responses pause except constrained keep-alives)
  -> APPROVAL_REQUIRED
  -> HUMAN_TAKEOVER (ordinary typed takeover)
  -> HUMAN_TAKEOVER (protected takeover required or active)
  -> CONNECTED
  -> COMPLETED | FAILED
```

Only explicit events may change state. Every transition is logged.

Gatekeeper verdict and latency metadata may enter the redacted event log, but classifier input does not. When `RELAY_DEBUG_CALL_CONTEXT=1` is explicitly set, the exact private Speaker and Gatekeeper payloads are written instead to an owner-only per-call file under `RELAY_DATA_DIR/debug/calls/`; this facility is disabled by default.

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

Payment is a field-by-field state machine, not one combined form: the representative asks for one field; Relay speaks a brief handoff line; the dashboard requires typed takeover; local TTS speaks only the scoped fake field; and the user hands control back before the next field can be detected. The old production `SECURE_LOCAL` fake-value form and `/secure-fields` endpoint are deprecated. The deterministic browser preview may retain its simulated form. The production bridge gates both Realtime directions, generates speech in memory with macOS AVSpeechSynthesizer, converts it to PCMU, publishes it only to the representative leg, and waits for Twilio's playback mark.

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
