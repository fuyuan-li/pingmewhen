# Relay P0 product requirements

## Product statement

> Give Relay a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

Relay is a goal-oriented agent. Phone calls are one action channel the agent may use to complete a broader task.

## Problem

Customer-service work forces people into synchronous voice interaction even though most of the call consists of waiting, repeated factual questions, and scripted exchanges. The user must remain attentive for the few moments when personal knowledge, judgment, authorization, or payment is required.

## Product thesis

Relay changes the interaction modality from voice-to-voice into visual/text-to-voice:

- The representative continues to hear a natural voice conversation.
- The user follows the call as a familiar chat interface.
- The user can do other work and respond asynchronously through text or one-click controls.
- Relay converts private user input into an appropriate spoken response.
- The user remains the authority for unknown facts and consequential actions.

## P0 audience

The hackathon demo targets a renter who wants to obtain comparable insurance quotes without personally conducting several repetitive calls.

The general product later supports any goal that requires one or more customer-service calls.

## P0 entry points

### `relay`

Starts the local Relay service and opens the localhost dashboard. The dashboard contains a task composer for a goal and supporting context.

### `relay demo`

Starts the same application in the single simulated renters-insurance demo mode.

No other CLI commands are required in P0.

## Demo journey

1. The user gives Relay the goal of obtaining renters insurance and supplies fake supporting information.
2. Relay plans calls to three simulated insurers.
3. Each call appears as a live chat conversation.
4. When Relay lacks an answer, it asks the representative for a moment and renders a quick-reply control.
5. The user may type a private instruction at any time.
6. Relay gathers the quotes and shows a factual comparison table without ranking or recommending.
7. The user selects an insurer and explicitly instructs Relay to proceed.
8. Relay calls the selected simulated insurer and continues the application.
9. The user personally confirms material representations and approves purchase.
10. Relay enters secure mode for a fake payment: cloud AI muted, transcription paused, macOS on-device TTS or user takeover available.
11. Relay resumes and records the non-sensitive policy outcome.

## Functional requirements

### Task intake

- Accept a goal.
- Accept supporting text and local files; PDF/image extraction may be limited to the demo fixtures in P0.
- Show the facts Relay plans to use and allow correction.

### Live conversation

- Representative utterances appear on the left.
- Relay utterances appear on the right.
- Private user instructions appear on the right with a distinct color and `Private to Relay` label.
- The user input box remains visible throughout the call.
- Private instructions are not spoken verbatim by default; Relay reformulates them in context.

### Structured user input

The call agent emits a constrained JSON schema mapped to prebuilt controls:

- single choice
- multiple choice
- yes/no confirmation
- date picker
- masked identifier
- address confirmation
- final action approval

Free text always uses the persistent chat input; it is not a generated component.

### Human control

- Unknown facts generate an immediate user request.
- Financial, contractual, legal, and final purchase actions require approval.
- A permanent Take Over control is visible during an active call.
- The user can return control to Relay after takeover when supported.

### AI disclosure

Relay begins calls with a concise disclosure such as:

> Hi, I’m Relay, an AI voice assistant speaking on behalf of Alex Chen. Alex is following our conversation live and responding through text because speaking isn’t convenient right now. Alex will personally provide requested information and approve every decision. Is it okay to continue this way?

### Insurance boundary

- Relay requests quotes using user-provided criteria.
- Relay records and presents factual differences.
- Relay does not interpret coverage, rank policies, recommend a carrier, or urge the user to apply.
- The user selects the carrier.
- A simulated licensed representative owns coverage explanation, application confirmation, binding, and payment.

### Secure mode

- Trigger before a fake card number or fake full SSN is exchanged.
- Mute or disconnect the cloud AI in both audio directions.
- Pause transcript and content logging.
- Offer macOS built-in on-device TTS and user takeover.
- Use fake values only in P0.
- Resume cloud AI only after the sensitive exchange ends.

### Logging and transcripts

- Persist call lifecycle, state transitions, transcript turns, private instructions, generated interaction schemas, user answers, approvals, tool calls, errors, and latency measurements.
- Store logs locally as structured JSONL.
- Store enough metadata to replay and debug a call.
- Mark speaker and visibility for every transcript item.
- Never store secure-mode content, card data, full SSNs, CVVs, PINs, passwords, API keys, or auth tokens.
- Record secure-mode start/end and redaction events without content.

## Success criteria

- A judge can install/run Relay and complete the simulated workflow without editing code.
- The visible conversation is driven by real runtime events, not a hard-coded transcript animation.
- The user successfully changes Relay behavior through a private instruction.
- At least one unknown question is answered through a generated quick-reply control.
- A consequential action is blocked until explicit approval.
- Secure mode visibly pauses the transcript and removes the cloud AI from the fake payment exchange.
- The final task record includes call history, factual quote comparison, selected carrier, and fake policy confirmation.

## P0 exclusions

- Real insurance transactions or recommendations
- Real payment or SSN data
- Arbitrary public calling
- Cross-platform local TTS
- Downloadable voice models
- Production billing
- Additional demo scenarios
- Additional CLI commands

