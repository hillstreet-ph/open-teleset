# Ops Copilot

The Copilot is the panel where you describe work in your own words and the product turns it
into something you can act on. One rule decides every behaviour in it: **the Copilot never
reaches another person or spends money until you press the button on that surface.**

It works in two tiers, and you reach it from the round bubble in the bottom-right corner of
every page or from the **Copilot** tab. The bubble opens a panel over whatever page you are
on, so you can draft without losing your place.

## Ask and Work

Both modes read and draft from the same rules. The difference is how far a write goes on its
own. The on/off switch sits on the left of the composer, inside the message row, and its label
is the mode that is active.

- **Ask** (switch off) answers from your workspace and drafts any state change. The Run button
  stays yours.
- **Work** (switch on) runs the safe internal writes — creating or pausing an automation — with
  no extra confirmation, and also applies the safe handoff steps: it opens the page a draft
  names, saves the template it drafted, or prefills the schedule, BulkSend or ops form and takes
  you there. It never presses an operator gate: sending, buying, joining and adding stay yours.

Only the two scheduler writes are autonomous. Internal reads and encrypted knowledge writes run
in both modes, which is why Ask can still "Review my accounts" or "Remember that our support
hours are 9 to 6". In Ask, a pending schedule write shows **Run now**, which performs that one
run in Work without flipping the switch (the browser setting is restored afterwards). A
finished run leaves no Run button behind, and the × on any draft dismisses it and brings the
**Superpowers** catalogue back.

Your choice is remembered in the browser (`ot_copilot_mode`), and the panel header says which
mode is on. When Work applies a handoff, the panel closes so you land on the surface with the
result in front of you.

## Draft and Agent engines

The panel header has a two-button switch. **Draft** is everything described above: the rules
read your words and prepare the surface. **Agent** hands the same prompt to your configured AI
provider and lets the model choose what to do from a fixed registry — the internal commands,
your curated skills and the connectors you saved. The choice is remembered in
`ot_copilot_engine`.

The registry is the whole world the model may call. `GET /api/copilot/agent/capabilities`
returns it, and the panel counts it in the agent intro. A name outside the registry is
reported back to the model as unknown and **never executed**.

- **Reads run inside the loop.** The model asks for a read command, the server runs it, and
  the model reads the result before answering. Each executed step appears under the reply with
  its tool name, a status badge (`ok`, `pending`, `error`) and one readable line, not a JSON
  dump.
- **Writes, skills and connector steps stop as a draft.** The model can ask for a scheduling
  write, `run_skill`, or `inspect_connector`; a write or a skill comes back as the same pending
  draft the Drafter produces, with **Run now** waiting for you. The model cannot press it.
- **The guard runs before the provider is contacted.** A refused prompt returns `Refused` with
  no model call at all, so a jailbreak attempt never spends a token.
- **No provider means the Drafter.** With no base URL, model or key configured, `POST
  /api/copilot/agent` answers with the same rule-based draft and labels itself *Built-in
  drafter*.

The loop is bounded: 6 model steps, 3 tool calls per step, a 30-second wall clock, 2000
characters of prompt and 1500 characters per tool result. Hitting a bound ends the turn with a
plain sentence in the panel instead of a half-finished action. The agent never reaches a send,
bulk, outreach, join or add endpoint — it has no tool for them, and the server refuses them
before any model call.

## Attachments

The paperclip in the panel attaches files to the next prompt. They are read in memory for that
turn only: up to 8 files, 4 MB each and 16 MB together. Any file type can be attached.

- **Read as text:** plain text and notes, PDF (up to 300 pages), Word, PowerPoint, Excel,
  OpenDocument (ODT/ODS/ODP), RTF, and ZIP archives of any of those.
- **Listed, not read:** images, video, audio and other binaries show the reason they cannot be
  read; they are never described as if they had been.

A document entry is read up to 2 MB, a ZIP text entry up to 512 KB, and a turn extracts up to
200 000 characters. Legacy binary formats (`.doc`, `.xls`, `.ppt`) are reported as unsupported
with that reason. Attachments are not written to disk and are not stored with the history; the
tray can be cleared with the × on each file.

"Import these files into my knowledge base" stores the full readable text in your own knowledge
base and reports entries, duplicates, masked values and skips. See
[AUTO_REPLY_AND_KNOWLEDGE.md](AUTO_REPLY_AND_KNOWLEDGE.md).

## Suggestions, follow-ups and enhance

Opening the panel offers **Suggested for you**, drawn first from your own notes, then from the
prompts you actually applied, then from real workspace state and the shipped starters. Each chip
fills the composer so you can edit before sending.

Below the suggestions, **Superpowers** lists every internal command the server exposes for this
operator, grouped as Workspace, Automation, Knowledge and Replies. The catalogue comes from
`GET /api/copilot/tools`; a command that changes state is marked `write`. Pressing a chip fills
the composer with that command's example and drafts it immediately, so a read answers in place
and a write becomes either a pending draft (Ask) or a completed action (Work).

Every draft also carries up to three **Next** chips — the concrete things worth doing once this
one is done. They fill the composer too; nothing is drafted or applied until you send.

**Enhance prompt** rewrites what you wrote into the explicit form the drafter reads best: it
names the template or schedule, quotes the message you gave, lists the recipients and approval
reference you already set, and asks for anything still missing. It never invents a recipient, a
message body or an approval id, and it shows what it added. Enhance needs no model call.

## Tier 1 — internal commands the server runs

These touch only your own encrypted workspace and never leave it. Reads and knowledge writes run
in both modes; the two scheduler writes need Work or the **Run now** button.

| Group | Command | What it does | Mode |
| --- | --- | --- | --- |
| Workspace | Review your accounts | Counts connected, online and erroring accounts. | Both |
| Workspace | Review your proxies | Proxy mode, failover and maintenance state, never a secret. | Both |
| Workspace | Show platform features | Which platform surfaces are switched on here. | Both |
| Workspace | Check your connectors | MCP connectors and whether a token is stored, never the token. | Both |
| Workspace | List your templates | Templates by category and the most used ones. | Both |
| Workspace | Give me a health summary | Accounts, cycles, features, connectors and recent errors in one read. | Both |
| Workspace | Show recent activity | The newest log lines from this workspace. | Both |
| Automation | List your automations | Every scheduled cycle with its action, state and next run. | Both |
| Automation | Schedule a member scrape | Creates a repeating scrape for a group your account can already read. | Work / Run now |
| Automation | Pause or resume an automation | Switches one scheduled cycle off or back on. Nothing else changes. | Work / Run now |
| Knowledge | Import the attached files | Stores the readable text of the attachments in your encrypted knowledge base. | Both |
| Knowledge | Save a note in your knowledge base | Stores the note, with duplicate detection. | Both |
| Knowledge | Search your knowledge base | Ranks your own notes against the question. | Both |
| Knowledge | Look for repeated wording | Proposes reusable phrases from the repeated ones. | Both |
| Replies | See which Auto Reply rule would answer | Matches a test message against your rules. Nothing is sent. | Both |

You reach them by writing prose — "remember that our support hours are 9 to 6", "pause the
member scrape automation", "which rule would answer 'are you open on Saturday?'" — by pressing a
**Superpowers** chip, or by pressing the matching preset. A command that cannot run reports why
in the panel instead of failing the request.

Page switches are the smallest safe step: "open the logs", "take me to the knowledge base"
and the other page names turn into a navigation draft that Work applies immediately and Ask
leaves for you to press.

## Tier 2 — prefilled handoff to the operational forms

For anything with consequences, the Copilot prepares the surface and stops at its existing
gate. It navigates you there, applies your own words inside hard bounds, re-asserts the
pacing defaults, and leaves every consent checkbox untouched.

| Surface | What the Copilot sets | What still waits for you |
| --- | --- | --- |
| Scraper | Member collection limit, default 500 | The scrape itself. |
| Adder | Delay, never below 35 seconds | The consent tick and **Start Member Adder**. |
| Auto Join | No per-run cap (every link), delay 38 | **Start Auto Join**. |
| BulkSend | Delay 45, no per-account cap | The opt-in list, the consent tick and **Start**. |
| Auto Reply | Interval 90 s, daily limit 20, rules mode | Saving settings and switching the listener on. |
| Account Factory | Price cap $0.50, OTP attempts 30 | **Run account factory**, and only where you are authorised to buy numbers. |
| Automation | Nothing | Refreshing status; the factory run itself is yours. |
| Knowledge | Nothing | **Export ZIP**, in the panel. |
| Inbox | Nothing | Opting an account in. Only accounts you opt in are watched. |

The bounds are not suggestions. `limit` accepts 0–2000 (0 queues every link), `delay` 0–300,
`perAccountLimit` 0–50 (0 means no cap), `maxPrice` $0.02–5, `otpAttempts` 1–120, `poll` 30–900
and `cap` 0–200, and the Adder and Auto Join surfaces narrow the delay further (35 s and 30 s
floors); the scraper keeps a 1-member floor. The Copilot can only tune a field the surface
already declares — it cannot invent a parameter.

The Adder and Auto Join surfaces are reachable from presets only, never from free text. Their
prose forms stay refused on purpose.

## What stays refused

Account creation, buying numbers, joining or adding people automatically, impersonating
someone, and cold or scraped outreach are outside what the Copilot will do at either tier. So
is anything whose purpose is to evade Telegram's limits or bans: greeting rotation, switching
accounts when a limit is hit, automatic warming, and proxy or fingerprint evasion. A
curated preset does not change that: it opens the gated form, it does not press it. These are
product boundaries shared by Ask and Work, not a mode you can switch off.

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
