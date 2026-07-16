# Relay

> **AI handles the conversation. You stay in control.**

---

# Overview

Relay is an AI-powered communication agent that can represent a user during customer service phone calls.

Instead of spending time waiting on hold, navigating IVR systems, repeating account information, or listening to long conversations, users simply define their objective and let Relay handle the interaction.

Throughout the call, Relay keeps the user continuously informed with a live activity feed, generates real-time summaries, requests user decisions whenever necessary, and allows the user to instantly take over the conversation at any time.

Unlike traditional voice assistants that attempt to replace humans, Relay is designed around **human supervision**.

The user is always in control.

---

# Vision

Humans shouldn't spend hours waiting for humans.

Relay removes people from the repetitive parts of customer service conversations while preserving human control whenever judgment, authorization, or personal preference is required.

Instead of replacing people, Relay optimizes **when people participate**.

---

# The Problem

Customer service interactions are inefficient because they require people to spend significant amounts of time on low-value tasks:

- Waiting on hold
- Navigating phone menus
- Repeating account information
- Listening to scripted explanations
- Providing routine verification
- Writing down case numbers
- Translating conversations
- Staying available for long periods just in case something happens

Ironically, the actual moments where the user is truly needed often last only a few minutes.

---

# Our Solution

Relay becomes the user's communication representative.

The user specifies:

- what needs to be accomplished
- relevant background information
- supporting documents
- account information
- preferences
- constraints

Relay then:

- places the phone call
- communicates with the customer service representative
- keeps working toward the user's goal
- continuously updates the user
- requests help only when necessary
- hands the conversation back whenever the user chooses

---

# User Workflow

## Step 1 — Create a Task

The user creates a new task.

Example:

```
Goal:
Cancel my Comcast internet service.

Reason:
I'm moving next week.

Preferred outcome:
Avoid cancellation fee if possible.

Constraints:
Do not accept any new annual contract.

Documents:
• Latest bill
• Account number
```

---

## Step 2 — AI Starts the Call

Relay initiates the phone call.

After the call connects, Relay becomes the active participant.

Relay automatically:

- introduces itself
- explains that it is assisting the customer
- begins working toward the requested objective

---

## Step 3 — Live Activity Feed

The user does **not** watch a transcript.

Instead, they watch a live activity stream describing exactly what the AI is doing.

Example:

```
10:32:04
Calling Comcast...

10:32:18
Connected.

10:32:22
Navigating billing menu.

10:33:40
Waiting for representative...

10:38:11
Representative joined.

10:38:25
Introduced reason for calling.

10:39:15
Representative confirmed account.

10:40:02
Representative offered a discounted plan.

10:40:08
Evaluating whether this matches your preferences...
```

This activity stream should feel like watching a human assistant work on the user's behalf.

---

# Live Dashboard

The dashboard consists of several continuously updated panels.

---

## Conversation Summary

Rather than displaying raw transcripts, Relay continuously maintains a concise summary.

Example:

```
Current Status

Representative verified account ownership.

Current issue:
Internet service cancellation.

Offer received:
$60/month for another 12 months.

Current AI strategy:
Attempting to negotiate a waiver of the cancellation fee.

Waiting for representative response.
```

---

## AI Reasoning

Relay should expose important decisions instead of behaving like a black box.

Examples:

```
Representative offered a discount.

Not recommending acceptance because:

• User requested cancellation
• User prefers no annual contract
```

or

```
Representative asked for account PIN.

Searching user-provided documents...

PIN not found.

User input required.
```

The user should always understand **why** Relay takes a particular action.

---

## Timeline

Every important event is recorded.

```
Representative joined

↓

Identity verified

↓

Cancellation requested

↓

Retention offer received

↓

Negotiation started

↓

Manager joined

↓

Fee waived

↓

Cancellation completed
```

This timeline later becomes the call history.

---

# User Intervention

Whenever Relay reaches a point requiring human judgment, it immediately notifies the user.

Examples include:

- financial commitments
- contract acceptance
- legal authorization
- unknown information
- personal preferences
- payment approval

---

## Push Notification

Example:

```
Decision Required

Representative offered:

$60/month
12-month contract

Your stated preference:
No annual contracts.

Choose:

[ Reject ]

[ Accept ]

[ Ask for a better offer ]

[ Join Call ]
```

The user should never need to reopen the app just to understand the context.

Every notification includes enough information for an informed decision.

---

# Instant Takeover

A permanent **Take Over** button remains visible throughout the call.

Pressing it immediately places the user into the live conversation.

The AI transitions into silent assistant mode while continuing to:

- generate summaries
- capture notes
- record action items
- provide suggested responses

The user may also hand control back to Relay later if appropriate.

---

# Real-Time Translation

Relay supports multilingual conversations.

Example:

Customer speaks English.

Representative speaks Spanish.

Relay translates both directions in real time.

The dashboard displays:

Original:

> Su solicitud ha sido procesada.

Translation:

> Your request has been processed.

Translation should also work when the user joins the conversation.

---

# Call Memory

Every completed call automatically generates structured notes.

Example:

```
Outcome

✅ Cancellation completed

Cancellation effective:
August 1

Cancellation fee:
Waived

Confirmation Number:
CX-482913

Representative:
Maria

Next Action:
Return modem within 14 days.
```

Users should never need to take notes during customer service calls again.

---

# Core Design Principles

## Human-in-the-loop

Relay never attempts to permanently replace the user.

Instead, it minimizes unnecessary human involvement while ensuring the user retains ultimate authority.

---

## Explainability

Users should always know:

- what Relay is doing
- why Relay is doing it
- what information Relay is using
- what decisions remain unresolved

---

## Transparency

Relay should never appear as an opaque autonomous system.

Every important action should be visible.

---

## User Control

The user can:

- interrupt at any time
- answer any question
- override any decision
- join the conversation immediately

Control is never taken away from the user.

---

## Context Awareness

Relay continuously incorporates:

- uploaded documents
- previous decisions
- user preferences
- earlier conversation history
- call objectives

to make better decisions throughout the interaction.

---

# Future Directions

Relay is not limited to customer service.

The same interaction model naturally extends to:

- insurance claims
- healthcare scheduling
- banking support
- government agencies
- legal intake calls
- travel rebooking
- technical support
- multilingual communication
- business phone calls

Ultimately, Relay becomes an intelligent communication layer between humans and every service that still depends on phone conversations.
