# PingMeWhen

> Hand off the call. PingMeWhen makes it for you, keeps you in the loop visually, and pings you in only when your knowledge, approval, or voice is actually needed.

PingMeWhen is a local-first, human-supervised voice agent. Give it a goal and the relevant context; it plans the call, dials in real time, and turns a synchronous phone conversation into a low-attention visual experience. You watch the live transcript, answer with quick replies or text, redirect strategy, approve anything consequential, listen to the audio, or take over and speak — by typing — whenever you want.

The name is the point: the agent works in the background and pings you only when you're needed.

## What it does

These are the product's highlights, ranked. See [`docs/HIGHLIGHTS.md`](docs/HIGHLIGHTS.md) for the full positioning.

- **You stay in control — it knows when to stop.** A model-driven planner turns your goal and any attached PDF or text into a plan you approve before anything happens. Prices, scheduling, enrollment, and payments pause and consult you; a budget or preference is context, never permission. The agent handles the mechanical part and hands the conversation back the moment judgment is needed.
- **It gives you your attention back.** It does the tedious, repetitive, waiting part and pings you in only for the few seconds that actually need a person — you're not stuck on the line.
- **Private by design.** Runs on your machine and your own Twilio/OpenAI keys — no hosted backend or shared credentials. Calls run over a secure tunnel with per-call, revocable capability tokens. Sensitive fields (card number, CVV, SSN) never touch the model: it's muted, you type the value, and it's spoken by on-device voice only — never sent to a model, never logged.
- **It talks to the real phone world.** Doctors, insurers, landlords, offices — the places that matter have no API, only a phone number. PingMeWhen reaches them over the actual phone network, calling real people.
- **Fully observable, always yours to take back.** The **Call Console** shows both sides of the call live and the **Private Workspace** is where you steer, privately. Listen in on the audio, or tap **Take over** and type — your words are spoken to the representative, and the interface drops into a private dark mode while you're on the line.

## Install and run

On macOS, install PingMeWhen and verify its on-device speech path with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/fuyuan-li/pingmewhen/main/install.sh | sh
pingmewhen
```

The installer adds `uv` when needed, installs PingMeWhen in an isolated tool environment, and silently verifies the same built-in macOS speech engine used for protected type-to-speak. There is no separate TTS model download. See [`docs/INSTALL.md`](docs/INSTALL.md) for details, updates, uninstalling, and source-development instructions.

From a local checkout:

```bash
uv sync --dev
uv run pingmewhen
```

Standard mode opens a first-run setup screen when any required credential is missing:

- `OPENAI_API_KEY`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`

Environment variables take precedence, so a local `.env` works too. Otherwise the dashboard stores credentials in an owner-only local file. PingMeWhen's maintainers never receive them and pay none of your provider costs.

The dashboard's **Models** control configures four roles independently: planning, Gatekeeper authority routing, Realtime voice, and transcription. Choices are stored locally. By default the dashboard runs at `http://127.0.0.1:8765`; set `RELAY_DATA_DIR` to relocate local data or `RELAY_PORT` to change the port.

Production calls start a `pycloudflared` tunnel alongside the local server so Twilio can reach the voice, status, and media webhooks over HTTPS/WSS. Each call gets separate random, call-bound capability tokens that are revoked when the call ends and redacted from logs; a supplied Twilio signature is also validated locally. Audio stays PCMU end to end between Twilio Media Streams and OpenAI Realtime.

<a id="how-codex-gpt-used"></a>

## How we used Codex and GPT-5.6

We used Codex and GPT models across the complete project lifecycle: discovering the opportunity, building the product, powering its live agent, and shaping the final story.

### 1. Discovering the opportunity with GPT-5.6

We used GPT-5.6 with web search to research adjacent products and competitors across customer-service voice agents and voice infrastructure, including Twilio, Vapi, Retell, and Bland AI. That research exposed an asymmetry: many AI agents now answer calls for businesses, but far fewer safely place calls for the consumer. It helped us identify the two barriers behind that gap—many everyday service providers have no API beyond a phone number, and their calls often involve personal information or decisions an AI must not make alone. This led to our central product thesis: the important challenge is not maximizing automation, but knowing when to hand control back to a human.

### 2. Pair programming with Codex

We used Codex as a repository-scale pair programmer rather than a one-shot code generator. The repo-level [`AGENTS.md`](AGENTS.md), together with the [PRD](docs/PRD.md), [technical design](docs/DESIGN.md), and [implementation plan](docs/IMPLEMENTATION_PLAN.md), gave Codex a durable product, architecture, privacy, and verification contract. Codex worked across the Python backend, browser UI, real-time media path, local storage, and tests to help us implement and refine the task state machine, Twilio–OpenAI bridge, Gatekeeper, human takeover, secure local speech, and approval boundaries. It also ran the test suite, checked cross-cutting security invariants, and kept implementation decisions and documentation aligned as the product evolved.

### 3. Powering PingMeWhen with specialized GPT models

The product separates four model roles so that deep reasoning, low-latency authority checks, live speech, and transcription can be optimized independently:

- **Planner — `gpt-5.6`:** understands an open-ended goal and attached documents, conducts multi-turn clarification, uses hosted web search for official contacts, and produces sourced, typed call plans through the Responses API and Pydantic Structured Outputs. The same planning layer produces factual post-call summaries and proposes the next step.
- **Gatekeeper — `gpt-5.4-nano` or `gpt-5.4-mini`:** classifies representative turns and private messages, detects missing facts and consequential decisions, and runs a separate fail-closed authority veto before the Speaker may continue.
- **Speaker — `gpt-realtime-2.1-mini` or `gpt-realtime-2.1`:** carries the natural live voice conversation over the real phone network. Automatic response creation is disabled so the backend decides when it is allowed to speak.
- **Transcriber — `gpt-4o-mini-transcribe` or `gpt-4o-transcribe`:** produces the live representative transcript used by the Gatekeeper and displayed to the user.

Models reason and communicate; application code owns plan approval, dialing, state transitions, consequential decisions, and protected-data boundaries. GPT-5.6 can propose what should happen, but it can never authorize its own plan.

### 4. Shaping the submission with GPT-5.6

Finally, we used GPT-5.6 to help organize and challenge our submission narrative. It helped us compare claims against the PRD, architecture notes, implementation evidence, and real-call workflow, avoid overstating unfinished capabilities, and test whether our positioning clearly expressed what made the product different. The team rewrote the final copy, verified every technical claim against the code, and made the final editorial decisions—including sharpening the story from a generic “AI that makes calls” into **“the AI that knows when to hand control back to a human.”**

Codex is central to the engineering workflow, but it is not an audio transport or a substitute credential for the Realtime API. PingMeWhen's runtime uses the local user's OpenAI API key.

## Important boundaries

- The agent discloses that it is an AI when it speaks, and never gives itself a fake human name.
- You select outcomes and approve consequential actions; a budget or preference never authorizes a decision on its own.
- Sensitive fields (card number, CVV, SSN) are handled locally: the cloud model is removed from the media path, transcription is paused, and the value you type is synthesized to speech on-device and injected only into the representative's call leg. It is never sent to the model or written to logs.
- Human takeover is text-to-speech today (you type; local voice speaks). Live browser-microphone takeover is not connected yet and is not represented as available.
- Attached context (PDF or text) is stored locally. Planning sends bounded extracted text to the configured model.
- PingMeWhen has one real-call product path; it does not ship a deterministic scenario or fake call mode.

## Repository map

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── docs/
│   ├── DECISIONS.md
│   ├── DESIGN.md
│   ├── IMPLEMENTATION_PLAN.md
│   └── PRD.md
├── src/relay_agent/          # internal package name (the shipped command is `pingmewhen`)
│   ├── app.py
│   ├── cli.py
│   ├── agentic_engine.py
│   ├── planner.py
│   ├── gatekeeper.py
│   ├── realtime_bridge.py
│   ├── telephony.py
│   ├── local_tts.py
│   ├── tunnel.py
│   └── static/
└── tests/
```
