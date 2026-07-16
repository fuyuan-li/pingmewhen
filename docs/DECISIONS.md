# Product decision record

This file captures the useful evolution of the idea without preserving every debate.

## 1. From autonomous caller to supervised delegation

The original idea emphasized an AI representative for customer-service calls. The project narrowed around human supervision: observe, steer, approve, take over, and audit.

## 2. From voice automation to modality conversion

The key novelty is not merely that AI speaks on the phone. Relay converts a synchronous voice interaction into a low-attention visual/text workflow while maintaining a voice presence for the representative.

## 3. Chat-style live interface

Raw live conversation is shown as left/right bubbles. Representative speech is left; Relay speech is right. User messages are private instructions on the right with distinct styling. A persistent text box supports interruption at any time, and structured quick replies reduce response latency.

## 4. Insurance as demo, not product boundary

The product is a goal-oriented agent that may use calls. The renters-insurance scenario demonstrates multiple calls, user input, comparison, approval, follow-up, and completion. It is not a dedicated insurance or calling product.

## 5. Insurance transaction boundary

Relay gathers user-requested quotes and shows factual comparison. It does not rank, recommend, solicit, bind, or receive commissions. The user chooses the carrier. A simulated licensed representative owns coverage explanation, application confirmation, binding, and payment.

## 6. AI disclosure

Relay follows OpenAI policy by disclosing that it is an AI voice assistant acting for a named user. It explains that the user is following live and will supply information and approvals. Later requests for input use natural language without repeating the disclosure.

## 7. Sensitive-data air gap

For fake card data and fake full SSNs, secure mode removes the cloud AI from both directions of the media path and pauses transcription. Each field is requested and spoken separately, with Relay resuming between fields. macOS built-in on-device TTS or human takeover handles the exchange. Browser playback is only a deterministic preview; production must inject local audio into the representative leg through the media gateway. No unsafe real-data cloud option is part of P0.

## 8. Local-first delivery

`relay` starts a local service and browser dashboard. `relay demo` runs the only demo. No additional commands are P0. The CLI launches the agent; the dashboard is the primary interaction surface.

## 9. Realtime authentication

Codex app-server can use ChatGPT/Codex authentication for Codex workloads, but that entitlement does not authorize arbitrary Realtime voice calls. Relay does not reroute the voice loop through Codex. P0 uses a limited hosted demo gateway so judges do not supply API keys.

## 10. Logging is a product requirement

Relay saves structured lifecycle events and transcripts locally for the live UI, debugging, evaluation, and simulator development. Secure-mode content and secrets are never logged. Sanitized patterns from properly disclosed research calls may inform synthetic simulator fixtures; raw conversations are not shipped.

## 11. P0 discipline

No Piper, downloaded voices, cross-platform local TTS, real insurance, real payment data, extra demo scenarios, extra CLI commands, or speculative authentication work.
