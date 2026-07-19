# PingMeWhen product requirements

## Product statement

> Give PingMeWhen a goal and the relevant context. It handles the phone calls, keeps you informed visually, and brings you in only when your knowledge, approval, or voice is needed.

PingMeWhen is a goal-oriented local agent. Real phone calls are one action channel for completing broader tasks.

## Problem and thesis

Customer-service work forces people into synchronous voice interaction even though much of a call is waiting, repetition, and scripted exchange. PingMeWhen changes that modality from voice-to-voice into visual/text-to-voice: the representative hears a natural caller, while the user follows a live transcript and returns only for personal knowledge, approval, judgment, or protected information.

## Audience

People who need to call real businesses or service providers but do not want to remain continuously engaged for the mechanical parts of the conversation.

## Entry point and ownership

- `pingmewhen` starts a localhost service and dashboard. `relay` remains a legacy alias.
- There is one real-call product path and no deterministic demo mode.
- Each installation is an independent single-user process using the user's own OpenAI and Twilio credentials.
- PingMeWhen operates no hosted account service, shared credential vault, or multi-tenant database.

## User journey

1. The user provides a goal and optional PDF context.
2. PingMeWhen clarifies missing facts and proposes a structured plan with sourced call targets.
3. The user revises or explicitly approves the plan.
4. PingMeWhen establishes the call tunnel and places approved calls.
5. The Call Console shows representative and PingMeWhen transcripts; the Private Workspace remains available for answers and direction.
6. Gatekeeper routes missing facts, offers, sensitive requests, and consequential choices to the user.
7. The user may listen to the call or take over by typing speech locally.
8. After a call, PingMeWhen returns a factual summary and continues the broader task conversation.

## Functional requirements

### Task intake and planning

- Accept a goal and supporting PDF context.
- Ask for missing information and let the user revise plans over multiple rounds.
- Show intended actions, phone numbers, purposes, and source provenance before execution.
- Require explicit approval before any call.
- Trust a user-provided contact as user-sourced; require an official source URL for researched contacts.

### Live calls

- Give every new representative a concise AI disclosure, represented-person identity, goal, and relevant known facts.
- Show representative utterances on the left and PingMeWhen utterances on the right.
- Keep private user/Gatekeeper messages in the Private Workspace and out of Speaker's raw context.
- Preserve one transcript tab per external connection and persist task context across calls.
- Create Speaker responses only after the backend's Gatekeeper and authority checks allow them.

### Human control

- Render structured prompts with quick replies, dates, months, and masked identifiers when applicable.
- Route unknown facts and consequential decisions to the user.
- Let the user privately redirect strategy at any time.
- Provide off-by-default, receive-only browser monitoring for both non-protected call audio directions.
- Provide keyboard type-to-speak takeover through local macOS speech; do not imply browser microphone support.

### Protected exchanges

- Trigger protected takeover for card number, CVV, expiration, full or last-four SSN, and date of birth.
- Stop both cloud audio directions and suppress transcript/content logging.
- Validate the requested field locally, render it with macOS on-device speech, and inject it only into Twilio.
- Resume Speaker only after local playback completes; never include the protected value in the resume context.

### Logging and security

- Persist task state, call lifecycle, redacted transcript turns, approvals, interactions, errors, and latency locally.
- Never store protected exchange content, card data, CVVs, full SSNs, passwords, API keys, Auth Tokens, or capability tokens.
- Authenticate every Twilio endpoint with a scoped per-call capability and validate a Twilio signature whenever one is present.
- Bind the service to `127.0.0.1` by default.

## Success criteria

- A new macOS user can install PingMeWhen with one command and finish first-run credential setup in the dashboard.
- The user can plan, approve, place, monitor, steer, and summarize a real phone call end to end.
- Missing facts and consequential choices visibly pause for the user.
- Protected values reach only local TTS and the representative call leg.
- The complete non-sensitive task history survives application restart.

## Current exclusions

- Browser-microphone takeover
- Cross-platform local TTS
- Downloadable voice models
- Hosted accounts or maintainer-funded provider usage
- Unapproved or unsourced calling
