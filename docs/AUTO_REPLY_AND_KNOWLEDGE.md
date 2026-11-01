# Auto Reply, the knowledge base and repeated wording

Four surfaces work together: Auto Reply answers inbound private chats on the accounts
you choose, the knowledge base holds the notes your own Copilot and reply model may read,
the Copilot keeps a history of every draft it produced, and repeated wording turns the
phrases that keep coming back into something reusable.

## Turn Auto Reply on

Auto Reply ships switched off, because it speaks as you on a real account. Turn it on under
**Integrations › Platform switches**. While that switch is off every Auto Reply page says so
and offers a button to the switch, rather than failing silently.

Then open **Auto Reply** and set the three numbers:

| Setting | Range | Default |
| --- | --- | --- |
| Check every | 30–900 seconds | 90 |
| Daily limit | 0–200 replies, 0 sends nothing | 20 |
| Watched accounts | up to 12 | none |

The master switch in that card is what starts and stops the listener; the page shows
**Running** or **Paused**, how many replies went out today against your limit, and the last
error if the worker hit one. **Run one check now** performs a single pass and reports the
counts, so you can see what a pass does without waiting for the interval.

## What it is allowed to answer

A reply only ever goes to a private chat whose newest message came from the other person.
Groups, channels, bots, first-ever messages and chats older than 30 minutes are never
answered. Every send still respects a 10-minute cooldown per chat, 20 seconds between
sends, the opt-out list and your daily limit. None of this is a way around Telegram's own
limits — keep the interval and the limit conservative.

## Two modes

**Rules — your wording sends on its own.** Add up to 12 rules. A rule matches on keywords
(up to 8, comma separated, case-insensitive) or on any incoming message, and its reply is
capped at 700 characters with `{name}` and `{first}` filled from the sender. The first
enabled rule that matches answers, so order matters less than turning the loose ones off.
Use **Test a message** to see which rule would answer and exactly what it would say; the
tester sends nothing.

**AI drafts for approval.** The model writes the wording, but nothing leaves Telegram until
you press **Send** on that draft in *Waiting for approval*. Editing a draft before sending is
allowed; the send re-checks the daily limit, the cooldown and the opt-out, and if Telegram
refuses, the draft stays waiting rather than being marked sent.

**Takeover.** Under *Conversations* you can see the chats being watched, the last message
seen and whether the chat is paused or muted. **Mute** stops auto-replies in that one chat;
**Resume** starts them again. An outgoing message you sent yourself is treated as a takeover:
the chat is paused, on the assumption that you are now handling it.

## The knowledge base

**Knowledge** is the note store the dashboard's own AI is allowed to read. Each operator
gets up to 200 entries of 4000 characters. The list shows a 180-character preview and the
real length; **Edit** opens the whole note, never the preview, so saving an edit cannot
silently shorten what is stored.

Anything shaped like a credential is masked before it is written, and the row then carries
a *credential masked* badge. The original value is not stored, not shown again and not
offered to the model — that is the point, so do not use this store as a secret vault.

*What the AI may collect* holds three switches: whether applied Copilot turns, sent replies
and your templates are fed into the store automatically. Turning one off stops that route;
notes you write by hand are unaffected. **Search** ranks your own notes, and **Remove all**
clears the store for you alone after a confirmation.

## Copilot history

Every draft you asked for is kept newest-first with its instruction, the wording and its
outcome. Marking a turn **Applied** records it and, when collection is switched on, offers
its wording to the knowledge base. **Discarded** records the opposite and collects nothing.
Neither action sends anything. **Clear** empties the history.

The Copilot remains a drafting tool only. Account factory, automatic joining, invented
identities and cold outreach are not offered here, and a draft still goes through the same
consent confirmation as any other send.

## Repeated wording

**Repeated wording** looks at the last 14 days of everything in your knowledge base and
Copilot history, and proposes a phrase three times or more as something reusable. It labels
each suggestion as a template, a reply, a task or a process, shows how many times it was
seen and which notes it came from.

Accepting is deliberately uneven:

- a **template** is saved, switched nothing, sends nothing;
- a **reply** becomes an Auto Reply rule that is created switched off, so it cannot answer
  until you turn it on;
- a **task** opens BulkSend with the wording filled in, where you still pick the recipients
  and confirm consent;
- a **process** opens the Copilot with the instruction filled in, so you still review the plan.

**Dismiss** keeps that one phrase from coming back; **Detect again** re-reads the corpus.
The list holds up to 20 suggestions.

## When it does not work

| What you see | What it means |
| --- | --- |
| "Auto Reply is switched off." | The platform switch is off. Turn it on under Integrations › Platform switches. |
| An account listed as skipped | No working client for that account — it needs to be connected again. |
| "Telegram did not accept the reply. Nothing was marked as sent." | The send failed. The draft is still waiting; fix the cause and press Send again. |
| Background listener: "Not running in this worker" | The poll loop lives in the worker process; use **Run one check now** to test from the dashboard. |
| A note shows *credential masked* | Something shaped like a secret was replaced on the way in. Re-word the note if you need that line. |
| No suggestions | Fewer than three repeats in the last 14 days, or the phrase was dismissed. |
