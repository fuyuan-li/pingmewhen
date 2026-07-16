# Relay implementation plan

The plan is deliberately narrow. Finish each vertical slice before expanding scope.

## Milestone 0 — foundation

- [x] Record final P0 product and architecture decisions.
- [x] Create Python package, `relay` entry point, localhost service, local event log, and initial test.
- [ ] Replace the placeholder dashboard with the live conversation shell.

Exit: `uv run relay` and `uv run relay demo` open a local application; tests pass.

## Milestone 1 — task and conversation UI

- Implement goal/context intake.
- Implement active-call bubble layout.
- Implement persistent private instruction input.
- Implement quick-reply schema validation and renderers.
- Implement local WebSocket event stream.
- Persist and reload transcript/event history.

Exit: a simulated event source drives the real UI, and private instructions change subsequent Relay output.

## Milestone 2 — insurer simulator

- Implement three synthetic insurer profiles.
- Implement quote questions, branching follow-ups, and factual results.
- Add at least one blocking unknown question.
- Add factual comparison table with no ranking or recommendation.
- Build conversation fixtures from sanitized patterns, not raw copied transcripts.

Exit: `relay demo` completes three distinct dynamic quote conversations and waits for the user’s carrier selection.

## Milestone 3 — Realtime voice and demo gateway

- Connect the simulated representative and Relay through real audio.
- Stream transcripts and speaker identity into the local UI.
- Implement AI disclosure at call start.
- Send private user responses back into the active conversation.
- Add interruption, timeout, disconnect, and retry handling.
- Log tool calls, latency, errors, and transcript turns.

Exit: the demo is driven by actual voice runtime events rather than scripted UI timing.

## Milestone 4 — approvals, takeover, and secure mode

- Block the final selection/purchase step on explicit approval.
- Add permanent Take Over control and conference state.
- Add secure-mode transition that disconnects cloud AI and pauses transcript logging.
- Use macOS built-in on-device TTS for fake card data.
- Route repetition or unexpected payment questions to takeover.
- Resume Relay and capture only non-sensitive confirmation.

Exit: a fake end-to-end policy purchase completes without sensitive values appearing in model context, transcripts, or logs.

## Milestone 5 — demo hardening

- Add deterministic reset and seed controls.
- Add reconnect/state restoration.
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

