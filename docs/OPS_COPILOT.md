# Ops Copilot

The Copilot is the panel where you describe work in your own words and the product turns it
into something you can act on. One rule decides every behaviour in it: **the Copilot never
reaches another person or spends money until you press the button on that surface.**

It works in two tiers.

## Tier 1 — internal commands the server runs

These touch only your own encrypted workspace, so the Copilot runs them immediately and
shows the result in the panel. Nothing leaves the workspace and nothing is sent.

| Command | What it does |
| --- | --- |
| Save a knowledge note | Stores the note in your own knowledge base, with duplicate detection. |
| Search the knowledge base | Ranks your own notes against the question and returns the top five. |
| Look for repeated wording | Re-reads your notes and proposes reusable phrases from the repeated ones. |
| Which Auto Reply rule would answer | Matches a test message against your rules. Nothing is sent. |

You reach them by writing prose — "remember that our support hours are 9 to 6" or "which
rule would answer 'are you open on Saturday?'" — or by pressing the matching preset. A
command that cannot run reports why in the panel instead of failing the request.

## Tier 2 — prefilled handoff to the operational forms

For anything with consequences, the Copilot prepares the surface and stops at its existing
gate. It navigates you there, applies your own words inside hard bounds, re-asserts the
pacing defaults, and leaves every consent checkbox untouched.

| Surface | What the Copilot sets | What still waits for you |
| --- | --- | --- |
| Scraper | Member collection limit, default 500 | The scrape itself. |
| Adder | Delay, never below 35 seconds | The consent tick and **Start Member Adder**. |
| Auto Join | Limit 24, delay 38 | **Start Auto Join**. |
| BulkSend | Delay 45, per-account limit 8 | The opt-in list, the consent tick and **Start**. |
| Auto Reply | Interval 90 s, daily limit 20, rules mode | Saving settings and switching the listener on. |
| Account Factory | Price cap $0.50, OTP attempts 30 | **Run account factory**, and only where you are authorised to buy numbers. |
| Automation | Nothing | Refreshing status; the factory run itself is yours. |
| Knowledge | Nothing | **Export ZIP**, in the panel. |
| Inbox | Nothing | Opting an account in. Only accounts you opt in are watched. |

The bounds are not suggestions. `limit` accepts 1–2000, `delay` 0–300, `perAccountLimit`
1–50, `maxPrice` $0.02–5, `otpAttempts` 1–120, `poll` 30–900 and `cap` 0–200, and the Adder
and Auto Join surfaces narrow the delay further (35 s and 30 s floors). The Copilot can only
tune a field the surface already declares — it cannot invent a parameter.

The Adder and Auto Join surfaces are reachable from presets only, never from free text. Their
prose forms stay refused on purpose.

## What stays refused

Account creation, buying numbers, joining or adding people automatically, impersonating
someone, and cold or scraped outreach are outside what the Copilot will do at either tier. A
curated preset does not change that: it opens the gated form, it does not press it. These are
product boundaries, not a mode you can switch.

## The preset runbook, and how it edits itself

Presets are the recipes the panel offers before you have to describe anything. The shipped
catalogue covers Templates, Schedules, Tasks, Outreach, Auto Reply, Automation, Knowledge and
Inbox, and each one carries a title, a description and the instruction it sends.

The runbook is yours to shape:

- **Edit** a shipped preset to change its wording for your shop. Your edit is stored as an
  override beside the shipped record, and the row is marked *modified*.
- **Add** your own, up to 40 per operator. A custom preset can be a draft or an internal
  command; it cannot name a command that does not exist.
- **Remove** one. A shipped preset is hidden, not deleted — nothing in the catalogue is lost.
- **Reset the runbook** to put the shipped wording back. Your usage counters survive a reset,
  because they describe what you actually did, not how the catalogue is configured.

Presets also improve themselves. Every turn you mark **applied** bumps that preset's usage
count, and the list is ordered by real use first, shipped order second. The row's badge shows
the count — `Knowledge · Export the knowledge base` with `3 applied`, or `edited` when only
the wording changed — so the runbook reads as what you actually do, not what shipped. The
recipes you actually reach for float to the top of the panel and the ones you never press
sink, with no configuration needed and each operator's ordering their own.

Applying a turn also offers its text to the knowledge base, so a good draft becomes material
for the next one.

## History

Every exchange is stored as a conversation turn (the last 100) with its outcome: drafted,
applied or discarded. The panel renders them as compact rows — `Draft · 623697d4`, `Auto Join
· dc1cc880` — that expand only when you open them, so a long history never becomes a long
scroll. **Clear history** empties the list; the knowledge base keeps whatever you chose to
collect.

## Export

The knowledge base exports as a ZIP of markdown files, one per note, under `documents/` and
`templates/`, plus an index. Credential-shaped values are scrubbed on the way out, so an
export never carries a secret even if a note predates the masking rule. See
[AUTO_REPLY_AND_KNOWLEDGE.md](AUTO_REPLY_AND_KNOWLEDGE.md).
