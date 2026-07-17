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

For fake card data and fake full SSNs, secure mode removes the cloud AI from both directions of the media path and pauses transcription. Each field is requested and spoken separately, with Relay resuming between fields. Browser playback is only a deterministic preview; production uses macOS built-in speech, injects PCMU into the representative leg, and waits for confirmed playback before reconnecting Realtime. Repeated requests route to `HUMAN_TAKEOVER`. No unsafe real-data cloud option is part of P0.

## 8. Local-first delivery

`relay` starts a local service and browser dashboard. `relay demo` runs the only demo. No additional commands are P0. The CLI launches the agent; the dashboard is the primary interaction surface.

## 9. Codex workflow and Realtime authentication

Codex is the repository-scale engineering agent used to implement, review, and verify Relay under the durable constraints in `AGENTS.md` and these product documents. GPT-5.6 has a separate runtime role in the standard private planner through the Responses API and Structured Outputs.

ChatGPT/Codex authentication applies to Codex workloads; it is not reused as authorization for Relay API calls. Relay is a bring-your-own-key local tool: each install uses the local user's OpenAI API key and Twilio Account SID, Auth Token, and phone number. No hosted Relay service receives credentials or funds usage. Deterministic demo mode remains credential-free.

## 10. Logging is a product requirement

Relay saves structured lifecycle events and transcripts locally for the live UI, debugging, evaluation, and simulator development. Secure-mode content and secrets are never logged. Sanitized patterns from properly disclosed research calls may inform synthetic simulator fixtures; raw conversations are not shipped.

## 11. P0 discipline

No Piper, downloaded voices, cross-platform local TTS, real insurance, real payment data, extra demo scenarios, extra CLI commands, or speculative authentication work.
