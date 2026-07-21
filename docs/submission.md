# Ping Me When — The AI That Knows When to Stop

## Inspiration

Most AI agents are designed around a single question: **“How much can we automate?”**

We started with the opposite question: **“When should an AI stop?”**

AI voice agents are increasingly answering calls *for businesses*. Far less attention has gone in the opposite direction: building an agent that can safely call those businesses *for the user*.

That direction runs into two fundamental barriers. First, AI is already effective when a service exposes an API, but many of the people and organizations we deal with in everyday life—doctors’ offices, handymen, local shops, landlords, and small service providers—are still phone-first. The phone number *is* their API. Second, these conversations often cross into personal territory: identity details, payment information, private circumstances, or choices that only the user has the authority to make. Giving an AI the ability to place a call is easy; deciding what it must never disclose or decide on its own is much harder.

And most of the call still consists of waiting, repeating information, asking routine questions, and collecting options. The moments that genuinely need a human—confirming an unknown fact, accepting an offer, making a financial commitment, or sharing protected information—are usually short and specific.

Today, the choice is usually between automating the entire interaction and risking an AI making decisions it should not make, or automating nothing and keeping the user trapped on the phone.

We built PingMeWhen to create a third option: let the AI handle the mechanical parts, keep the entire process visible, and ping the user only when their knowledge, approval, judgment, or voice is genuinely needed.

> We did not build an AI that simply does more. We built one that knows when to stop.

## What it does

PingMeWhen is a **local-first, human-supervised task agent** that uses real phone calls as an action channel.

The user gives it a goal and optional PDF or text context. PingMeWhen clarifies missing details, researches official contact information, and creates a structured call plan. Nothing happens until the user reviews and explicitly approves that plan.

Once approved, PingMeWhen calls real businesses over the phone network and speaks naturally on the user’s behalf while clearly disclosing that it is an AI. The user does not have to remain on the line: they can follow both sides of the conversation through a live transcript, listen in through a receive-only audio monitor, or privately redirect the agent at any time.

A separate **Gatekeeper** watches the conversation for moments the AI must not handle alone—unknown facts, offers, permissions, financial or contractual decisions, and sensitive requests. When one appears, the call pauses and PingMeWhen asks the user a focused question through its Private Workspace. The answer is converted into a typed, confirmed context update rather than exposing the user’s raw private message to the voice model.

The user can also take over at any moment by typing what they want spoken. For protected information such as a card number, CVV, SSN, or date of birth, PingMeWhen removes the cloud model from both audio directions, pauses transcription and logging, and speaks the value using on-device macOS speech. The sensitive value goes only to the phone call—never to the AI model and never to a log.

The result is a new interaction model: **visual and asynchronous for the user, while remaining a natural voice conversation for the person on the other end.**

## How we built it

PingMeWhen runs as a local FastAPI application with a browser-based dashboard. Task state, credentials, transcripts, approvals, and event history are stored locally using SQLite and machine-local files.

The private planner uses the OpenAI Responses API with Pydantic Structured Outputs. It turns goals and document context into typed plans, clarification questions, sourced contacts, and executable phone actions. Application code—not model output—owns every approval and execution boundary.

For live calls, Twilio places the PSTN call and streams PCMU audio through Media Streams. PingMeWhen bridges that audio to OpenAI Realtime for natural speech-to-speech interaction.

We split call-time intelligence into two roles:

- **Speaker** is the only live audio model and the only AI the representative hears.
- **Gatekeeper** is a text-only authority layer that decides whether the conversation may continue or must return to the user.

A second, fail-closed authority check examines every proposed continuation. If either check is uncertain or malformed, PingMeWhen consults the user instead of guessing.

The backend acts as the control plane. It disables automatic model responses, explicitly controls when the Speaker may speak, maintains the durable call state machine, and keeps private user messages outside the Speaker’s raw context.

Because the application runs locally but Twilio requires public webhooks, we create a session-long Cloudflare tunnel. Every call receives separate, revocable capabilities for its voice, status, media, and browser-listening endpoints. Twilio signatures are validated when present, and capability tokens are redacted from logs.

For protected handoffs, we use macOS `AVSpeechSynthesizer` locally, convert the audio to PCMU, and inject it directly into the Twilio call leg while cloud audio and content logging remain gated.

## Challenges we ran into

The hardest problem was not making the AI talk. **It was controlling when it was allowed to talk.**

A real-time voice model naturally wants to answer immediately, but PingMeWhen must first transcribe the representative, classify the request, check the user’s authority boundary, and only then permit a response. We had to disable automatic response creation and make the backend explicitly coordinate every turn without making the call feel unnatural.

Keeping public and private context separate was another major challenge. The user needs to steer the agent during a live call, but their private words should not be copied directly into the representative-facing conversation. We created a typed context-update layer that preserves the user’s intent while preventing accidental disclosure.

Protected information required an entirely separate media path. Muting only the model’s response was not enough: we needed to gate inbound and outbound cloud audio, pause transcription and content logging, synthesize the value locally, wait for phone playback to complete, clear it from memory, and resume without leaking it into later context.

We also had to reconcile a local-first architecture with real telephony. Twilio needs reachable HTTPS and WebSocket endpoints, while PingMeWhen should remain a single-user localhost application. That led to the tunnel lease, per-call capability, signature-validation, revocation, and log-redaction architecture.

Finally, we had to design an interface where planning, private consultation, live transcripts, multiple calls, audio monitoring, and human takeover could coexist without turning the call into a black box.

## Accomplishments that we're proud of

We are most proud that **“the AI knows when to stop” is not only a tagline. It is enforced by the application’s architecture and visible in the product.**

Consequential decisions are hard stops in a durable state machine, not suggestions hidden inside a prompt. A budget or stated preference constrains the conversation but never authorizes the AI to accept an offer.

We built an end-to-end real-call path: planning, explicit approval, PSTN dialing, live speech, Gatekeeper escalation, private user input, typed takeover, protected local speech, persistent transcripts, and post-call summaries all work as one product.

We are also proud of the protected handoff system. Sensitive values never touch the AI model, transcript, or event log, yet the user can still complete the exchange without verbally joining the call.

PingMeWhen is fully observable and always recoverable by the user. Every external conversation has its own transcript, the user can listen without affecting the call path, and takeover remains available at all times.

Finally, we shipped it as a local-first, bring-your-own-key macOS application with a one-command installer. There is no hosted PingMeWhen backend, shared credential vault, multi-tenant database, or maintainer-funded provider account.

## What we learned

We learned that **useful autonomy is not the same as maximum autonomy.**

The most important component of an agent may be the mechanism that prevents it from acting. Prompts alone are not a sufficient authority boundary; consequential actions need explicit application-owned states, validated schemas, durable approvals, and fail-closed behavior.

We also learned that human-in-the-loop design works best when the human is not treated as an emergency fallback. In PingMeWhen, the user and AI operate in parallel: the agent manages the ongoing call while the user supplies brief moments of knowledge or judgment. The handoff is part of the normal workflow, not a failure mode.

Privacy also became an architectural property rather than a policy statement. If information must not reach a model, redacting it afterward is too late. The model must be removed from the media path before the information is spoken.

Most importantly, we learned that trust comes from restraint, observability, and reversibility. Users are more willing to delegate when they can see what is happening, understand why they are being consulted, and take control at any moment.

## What's next for Ping Me When — The AI That Knows When to Stop

Next, we want to expand PingMeWhen from phone-call execution into a broader local task agent, with additional user-approved action channels while preserving the same human-authority model.

We plan to add cross-platform protected speech, richer accessibility options, browser-microphone takeover, and support for more complex call flows such as automated phone menus. We also want to improve multi-call comparison so PingMeWhen can normalize quotes, identify meaningful differences, and bring the user back only for the final judgment.

Longer term, PingMeWhen could coordinate calls with documents, calendars, email, and follow-up tasks—but every new capability will follow the same rule: goals and preferences guide the agent; they never silently authorize consequential decisions.

The future of agents should not be measured only by how long they can operate without us. It should also be measured by how precisely they recognize the moments when we matter.

> The hard part was not making the AI capable enough. It was teaching it when to hand control back to a human.
