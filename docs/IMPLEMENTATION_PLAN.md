# PingMeWhen implementation plan

Status: finalized for the `v0.1.0` real-call release.

## Completed foundation

- [x] Package the localhost FastAPI service and `pingmewhen` executable.
- [x] Persist single-user credentials, contexts, events, and complete task snapshots locally.
- [x] Add first-run BYOK onboarding for OpenAI and Twilio credentials.
- [x] Add model-driven private planning with Structured Outputs, PDF context, contact provenance, and explicit approval.
- [x] Remove the deterministic scenario runtime; keep one real-call product path.

## Completed real-call execution

- [x] Start a session-long `pycloudflared` tunnel and report public health before dialing.
- [x] Authenticate Twilio voice, status, media, and listener endpoints with scoped per-call capabilities.
- [x] Place approved PSTN calls and bridge Twilio PCMU audio to OpenAI Realtime.
- [x] Persist incremental representative/PingMeWhen transcripts and per-call history tabs.
- [x] Split live behavior into Speaker plus a text-only Gatekeeper and authority veto.
- [x] Route missing facts, offers, approvals, and consequential decisions to the Private Workspace.
- [x] Keep routing/delivery failures nonfatal and preserve retryable pending interactions.

## Completed human control and privacy

- [x] Add structured quick replies, date/month controls, and masked identifiers.
- [x] Add general keyboard type-to-speak takeover through macOS local TTS.
- [x] Gate cloud audio and logging during protected payment, SSN, and date-of-birth exchanges.
- [x] Add receive-only browser monitoring with bounded best-effort audio fan-out.
- [x] Return a dedicated model-generated post-call summary to the Private Workspace.

## Installation and release

- [x] Add a one-line curl installer that installs uv and PingMeWhen.
- [x] Verify the exact macOS on-device speech path after installation.
- [x] Document first-run credential requirements and local ownership.
- [x] Verify an isolated wheel installation, CLI startup, and local TTS check on macOS.
- [x] Publish the tagged `v0.1.0` GitHub release with wheel and source-distribution assets.
- [x] Rename the GitHub repository from `relay` to `pingmewhen` and update installer URLs.

## Deferred

- Browser-microphone takeover
- Cross-platform local TTS
- Hosted accounts, billing, or multi-tenant operation
- Non-phone action tools
- General ChatGPT login for Realtime unless officially supported
