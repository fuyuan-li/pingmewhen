# PingMeWhen decisions

## Product and delivery

- PingMeWhen is a goal-oriented local agent; phone calls are an action channel, not the entire product definition.
- The dashboard is the primary interface. `pingmewhen` launches it and `relay` remains a legacy executable alias.
- The shipped product has one real-call path. The deterministic insurer scenario and credential-free demo mode were removed.
- Each user brings an OpenAI API key and Twilio Account SID, Auth Token, and phone number. PingMeWhen operates no hosted credential or billing service.

## Human authority

- Application code, not a model, owns plan approval and call execution boundaries.
- Speaker is the sole live voice model. A separate text Gatekeeper and authority veto decide when a representative turn must return to the user.
- Budgets, preferences, and task goals are constraints, not authorization to accept an offer or make another consequential decision.
- The Private Workspace remains the only user/Gatekeeper channel; raw private text is never exposed to Speaker.

## Audio and privacy

- Twilio Media Streams and OpenAI Realtime use PCMU audio end to end.
- Browser listening is receive-only, capability-scoped, bounded, and excluded from protected spans.
- Takeover is keyboard type-to-speak. Browser microphone takeover is not connected.
- Protected values use macOS `AVSpeechSynthesizer`, injected only into the Twilio call leg while cloud audio and content logging are gated.
- PingMeWhen uses the speech engine built into macOS. It does not download or bundle a voice model.

## Security and storage

- Tasks, credentials, context, logs, transcripts, and optional call-debug traces remain machine-local.
- Each call receives independent voice, status, media, and listen capabilities; terminal status revokes them.
- A present Twilio signature is validated with the official SDK. An invalid signature is rejected.
- Secure-mode content, provider credentials, and capability tokens are never logged.

## Installation

- The supported one-line installer checks or installs uv, installs the package with `uv tool install`, and verifies local TTS silently.
- macOS is the supported platform while local protected speech depends on AVFoundation.
