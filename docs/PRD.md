# PingMeWhen P0 product requirements

## Product statement

> Give PingMeWhen a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

PingMeWhen is a goal-oriented agent. Phone calls are one action channel the agent may use to complete a broader task.

## Problem

Customer-service work forces people into synchronous voice interaction even though most of the call consists of waiting, repeated factual questions, and scripted exchanges. The user must remain attentive for the few moments when personal knowledge, judgment, authorization, or payment is required.

## Product thesis

PingMeWhen changes the interaction modality from voice-to-voice into visual/text-to-voice:

- The representative continues to hear a natural voice conversation.
- The user follows the call as a familiar chat interface.
- The user can do other work and respond asynchronously through text or one-click controls.
- PingMeWhen converts private user input into an appropriate spoken response.
- The user remains the authority for unknown facts and consequential actions.

## P0 audience

The hackathon demo targets a renter who wants to obtain comparable insurance quotes without personally conducting several repetitive calls.

The general product later supports any goal that requires one or more customer-service calls.

## P0 entry points

### `relay`

Starts the local PingMeWhen service and opens the localhost dashboard. If required provider credentials are absent, the dashboard first collects and stores them only on the user's machine. It then shows the task composer for a goal and supporting context.

### `relay demo`

Starts the same application in the single simulated renters-insurance demo mode.

No other CLI commands are required in P0.

## Local ownership model

- PingMeWhen is installed and run as a fully independent, single-user local process.
- The user supplies their own OpenAI API key and Twilio Account SID, Auth Token, and phone number.
- PingMeWhen operates no shared backend, account system, credential vault, or multi-tenant database.
- Provider usage is charged only to the user's own accounts.
- `relay demo` stays deterministic and credential-free.

## Demo journey

1. In the private planning stage, the user gives PingMeWhen the goal and optional supporting documents.
2. PingMeWhen asks for missing facts, proposes companies and verified contact details, and lets the user revise the plan over multiple rounds.
3. PingMeWhen starts no call until the user explicitly approves the finalized plan.
4. The Call Console gradually widens beside the Private Workspace. The Private Workspace narrows but stays active as the only private user/PingMeWhen channel; the Call Console shows only the external Speaker/representative transcript and call controls.
5. When PingMeWhen lacks an answer, it asks the representative for a moment and renders a quick-reply control.
6. The user may barge in at any time; PingMeWhen reformulates the private instruction and inserts it before the next call turn.
7. After quote calls finish, PingMeWhen returns to the Private Workspace and shows a factual comparison without ranking or recommending. The Call Console collapses into a vertical right-edge history bookmark that can reopen the per-call tabs.
8. The user selects an insurer and explicitly approves the callback in that planning conversation.
9. PingMeWhen switches back to the live-call panel, calls the selected simulated insurer, and continues the application.
10. The user personally confirms material representations and approves purchase.
11. PingMeWhen enters secure mode for payment: cloud AI muted, transcription paused, and card number, expiration, and CVV requested in separate local-TTS handoffs. The deterministic demo uses fake values; production may speak the user's real locally entered value without cloud or log exposure.
12. PingMeWhen resumes between each requested field, then returns to planning and records only the non-sensitive policy outcome.

## Functional requirements

### Task intake

- Accept a goal.
- Accept supporting text and local files; PDF/image extraction may be limited to the demo fixtures in P0.
- Show the facts PingMeWhen plans to use and allow correction.
- Do not treat arbitrary clarification text as a required fact. Validate typed addresses and ask for confirmation before using PDF-extracted candidates.
- Keep PDF attachment available inside the planning conversation, not only on the initial form.

### Planning boundary

- Planning is a private conversation between the user and PingMeWhen. It fills the workspace when no call is active and remains interactive beside an active call as the private user/Gatekeeper channel.
- PingMeWhen may request missing facts or documents before proposing external actions.
- The user can revise companies, questions, constraints, and ordering over multiple rounds.
- The plan shows intended calls and contact details before execution.
- An explicit approval closes planning and opens the live-call interface.

### Live conversation

- Representative utterances appear on the left.
- PingMeWhen utterances appear on the right.
- Private user instructions remain in the Private Workspace on the right with a distinct color and `Private to PingMeWhen` label; Gatekeeper replies remain on the left.
- The Private Workspace input box remains visible throughout the call. The external Call Console has no private composer.
- Private instructions are not spoken verbatim by default; PingMeWhen reformulates them in context.
- Call turns appear incrementally rather than being dumped as a completed transcript.
- A barge-in is inserted ahead of the next queued call turn so it changes the visible conversation naturally.
- Every new representative receives a fresh disclosure, goal, and relevant call context even though PingMeWhen retains task memory across calls.
- Every external connection has its own persistent transcript tab. A later callback to the same company opens a new tab and does not assume the same representative answered.

### Structured user input

The call agent emits a constrained JSON schema mapped to prebuilt controls:

- single choice
- multiple choice
- yes/no confirmation
- date picker
- month picker
- masked identifier
- address confirmation
- final action approval

Free text always uses the persistent chat input; it is not a generated component.

### Human control

- Unknown facts generate an immediate user request.
- Financial, contractual, legal, and final purchase actions require approval.
- A permanent keyboard Take Over control is visible during an active production call and lets the user type speech through local macOS TTS without a browser microphone.
- The user can return control to PingMeWhen after takeover.
- A separate, off-by-default Listen control lets the user monitor both non-protected call audio directions in the browser. It is receive-only and never injects browser audio into the call.
- The deterministic UI must say `Simulate takeover · no audio` and must not imply that production call audio is connected.

### AI disclosure

PingMeWhen begins calls with a concise disclosure such as:

> Hi, I’m PingMeWhen, an AI voice assistant speaking on behalf of Alex Chen. Alex is following our conversation live and responding through text because speaking isn’t convenient right now. Alex will personally provide requested information and approve every decision. Is it okay to continue this way?

### Insurance boundary

- PingMeWhen requests quotes using user-provided criteria.
- PingMeWhen records and presents factual differences.
- PingMeWhen does not interpret coverage, rank policies, recommend a carrier, or urge the user to apply.
- The user selects the carrier.
- A simulated licensed representative owns coverage explanation, application confirmation, binding, and payment.

### Secure mode

- Trigger before a card number, CVV, expiration date, full or last-four SSN, or date of birth is exchanged.
- Mute or disconnect the cloud AI in both audio directions.
- Pause transcript and content logging.
- Offer macOS built-in on-device TTS and user takeover.
- Keep the deterministic demo fake-only; validate production values locally for the detected protected field.
- Resume cloud AI only after the sensitive exchange ends.
- Show field-specific local controls: date/month pickers where applicable and masked numeric inputs with visible format/length hints for identifiers.

### Logging and transcripts

- Persist call lifecycle, state transitions, transcript turns, private instructions, generated interaction schemas, user answers, approvals, tool calls, errors, and latency measurements.
- Store logs locally as structured JSONL.
- Store enough metadata to replay and debug a call.
- Mark speaker and visibility for every transcript item.
- Never store secure-mode content, card data, full SSNs, CVVs, PINs, passwords, API keys, or auth tokens.
- Record secure-mode start/end and redaction events without content.

### Telephony security

- Start the local HTTPS tunnel with standard PingMeWhen, visibly probe public health before an approved call, and treat the probe as diagnostic rather than a dialing gate.
- Supply voice and status webhook URLs dynamically on each Twilio call creation request.
- Validate every inbound Twilio webhook with Twilio's official request validator and the user's Auth Token.
- Stop the tunnel after the last active call or when PingMeWhen exits.
- Never expose or log the OpenAI API key or Twilio Auth Token.

## Success criteria

- A judge can install/run PingMeWhen and complete the simulated workflow without editing code.
- The visible conversation is driven by real runtime events, not a hard-coded transcript animation.
- The user successfully changes PingMeWhen behavior through a private instruction.
- At least one unknown question is answered through a generated quick-reply control.
- A consequential action is blocked until explicit approval.
- Secure mode visibly pauses the transcript and removes the cloud AI from the fake payment exchange.
- The final task record includes call history, factual quote comparison, selected carrier, and fake policy confirmation.

## P0 exclusions

- Real insurance transactions or recommendations
- Arbitrary public calling
- Cross-platform local TTS
- Downloadable voice models
- Production billing
- Additional demo scenarios
- Additional CLI commands
