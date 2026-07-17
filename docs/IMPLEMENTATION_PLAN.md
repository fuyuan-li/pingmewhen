# Relay implementation plan

The plan is deliberately narrow. Finish each vertical slice before expanding scope.

## Milestone 0 — foundation

- [x] Record final P0 product and architecture decisions.
- [x] Create Python package, `relay` entry point, localhost service, local event log, and initial test.
- [x] Replace the placeholder dashboard with the live conversation shell.

Exit: `uv run relay` and `uv run relay demo` open a local application; tests pass.

## Milestone 1 — task and conversation UI

- [x] Implement goal/local-PDF context intake.
- [x] Implement active-call bubble layout.
- [x] Implement persistent private instruction input.
- [x] Implement quick-reply schema validation and renderers.
- [x] Separate private planning from the live-call monitor with an explicit approval boundary.
- [x] Pace simulated turns from a backend queue and support barge-in ahead of the next turn.
- [x] Preserve separate planning and live-call histories and return to planning for quote comparison.
- [x] Validate typed addresses and allow PDF attachment during planning.
- Implement local WebSocket event stream.
- [x] Persist transcript and workflow events to the local JSONL log.
- [x] Persist and reload active task state after a restart with machine-local SQLite.
- [x] Add a Responses API planner for standard `relay` mode with schema-validated plans and hard approval boundaries.
- [x] Render general action plans independently of the insurance demo schema.
- [x] Connect plan generation to bounded hosted web search for sourced official contact details.
- [x] Add local first-run BYOK onboarding for OpenAI and Twilio credentials.
- [x] Persist credentials in an owner-only local file with environment-variable precedence.
- [x] Add lazy `pycloudflared` lifecycle and dynamic per-call Twilio webhook URLs.
- [x] Validate Twilio voice/status webhooks with the official SDK helper.
- [x] Connect approved, sourced E.164 phone-call actions to the telephony control plane.

Exit: a simulated event source drives the real UI, and private instructions change subsequent Relay output.

## Milestone 2 — insurer simulator

- [x] Implement three synthetic insurer profiles.
- [x] Implement quote questions, branching follow-ups, and factual results.
- [x] Add at least one blocking unknown question.
- [x] Add factual comparison table with no ranking or recommendation.
- [x] Restart representative-facing context with a fresh introduction and task brief on every call.
- [x] Render each external connection in a persistent transcript tab, including a separate application-callback tab.
- [x] Animate the Call Console width between hidden, live parallel, expanded-history, and vertical-bookmark states while the Private Workspace resizes inversely.
- [x] Keep the Private Workspace interactive during live calls and restrict the Call Console to the external transcript and controls.
- [x] Route raw private call messages through Gatekeeper and expose only typed confirmed context updates to Speaker after a `session.updated` acknowledgement.
- Build conversation fixtures from sanitized patterns, not raw copied transcripts.

Exit: `relay demo` completes three distinct dynamic quote conversations and waits for the user’s carrier selection.

## Milestone 3 — Realtime voice and local media bridge

- Connect the simulated representative and Relay through real audio.
- Add a browser WebRTC microphone leg and shared media conference for genuine takeover.
- Route the simulated representative as a separate audio participant before enabling general PSTN execution from the task engine.
- [x] Bridge approved PSTN calls through Twilio Media Streams and OpenAI Realtime using PCMU audio.
- [x] Stream completed transcripts and speaker identity into durable task state and the local UI.
- [x] Implement AI disclosure and a fresh purpose statement at every call start.
- [x] Send private user responses into the active Realtime conversation.
- [x] Gate every representative turn through a text-only classifier before manually creating the Speaker response.
- [x] Accumulate call-local user updates and reuse the existing waiting prompt for unanswerable turns.
- [x] Add periodic constrained keep-alive speech while waiting for private user input.
- Add broader interruption, timeout, disconnect, and retry handling.
- Log tool calls, latency, errors, and transcript turns.

Exit: the demo is driven by actual voice runtime events rather than scripted UI timing.

## Milestone 4 — approvals, takeover, and secure mode

- [x] Block the final selection/purchase step on explicit approval in the simulator.
- [x] Add truthfully labeled simulated takeover/pause state.
- Replace simulated takeover with a real audio-track switch only after the shared media bridge is operational.
- [x] Add field-by-field simulated secure-mode transitions and deliberate transcript gaps.
- [x] Use the browser/OS local speech facility separately for fake card number, expiration, and CVV in the deterministic preview.
- [x] Inject locally synthesized PCMU only into the representative call leg while both Realtime directions are gated.
- [x] Route repeated protected-field requests to `HUMAN_TAKEOVER` without repeating the value.
- [x] Resume Relay only after Twilio confirms local playback and capture only non-sensitive content.

Exit: a fake end-to-end policy purchase completes without sensitive values appearing in model context, transcripts, or logs.

## Milestone 5 — demo hardening

- Add deterministic reset and seed controls.
- Add browser reconnect to an existing persisted task.
- Add redaction, state-machine, approval, and simulator tests.
- Verify a clean-machine `uvx`/`uv` installation path.
- Prepare a short demo script and fallback recording.
- Review logs to confirm no test secrets leak.

Exit: a judge can run the complete flow reliably without source edits or private credentials.

## Deferred

- Real insurers, cards, or SSNs
- Production insurance/legal review
- Cross-platform TTS or downloadable voices
- Additional task demos
- Billing and subscriptions
- Arbitrary calling
- General ChatGPT login for Realtime unless officially supported
