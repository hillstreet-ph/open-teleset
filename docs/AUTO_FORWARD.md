# Auto Forwarder

Auto Forwarder copies new messages from a chat an account can read into a chat you own.
It is a mirror between your own chats, not outreach: the account must already be able to
read the source, and the destination must already exist. Nothing is sent to anyone else.

## Turn it on

Auto Forwarder ships switched off, because forwarding happens on a real account. Turn it on
under **Integrations › Platform switches**. While that switch is off, the Auto Forwarder page
says so and offers a button to the switch instead of showing a load error.

Then open **Auto Forwarder** and set:

| Setting | Range | Default |
| --- | --- | --- |
| Forward automatically | on / off | off |
| Check every | 30–900 seconds | 60 |

The master switch starts and stops the listener; the card shows **Running** or **Paused**,
the number of rules, and the worker's last pass and last error. **Run one pass now** performs
a single pass immediately and reports the counts, so you can see what a pass does without
waiting for the interval. A shorter interval means more Telegram reads per account; keep it
conservative.

## Rules

A rule names one account, one source and one destination. Up to 10 rules are kept.

- **Source and destination** take `@username`, `t.me/username` or a numeric chat id. A private
  invite link is not a valid source; join the chat first and use its username or id.
- The source and destination must be different chats.
- **Rule enabled** only decides whether the listener looks at that rule; the master switch
  still has to be on.

Editing a rule's route starts a fresh cursor. The old last-seen id means nothing in a
different chat, so the next pass forwards what is new from that point rather than replaying
the source's history.

## What a pass does

For each enabled rule the listener reads at most the 20 newest messages newer than the last
id it recorded, then forwards them in one call. That recorded id only moves forward after a
successful forward, so a crash, a restart or a refused call cannot duplicate history.

- A Telegram flood wait is recorded **per account**; only that account's rules wait, and the
  date is shown as plain words rather than a raw exception.
- An error is recorded by its type only — account not connected, chat not found, flood wait,
  Telegram refused the forward — never by its message text, which can carry request details.
- **Recent activity** lists the newest 20 events: when, which rule, which account, how many
  messages, and the verdict.

## Limits worth knowing

Forwarding is a read plus a forward per message for the account that owns the rule. Telegram
counts these against that account like any other action. Keep the interval at a minute or
more, keep the number of rules small, and do not point several rules at the same live chat
from the same account.

If the account cannot connect — an expired session, a proxy problem, an account-level
cooldown — the rule reports **Account is not connected** and the rest of the rules keep
going. Fix the account under **Accounts** (or re-login it), then run a pass.

## Storage

Each operator's configuration is one encrypted record under the runtime settings store:
the switch, the interval, the rules, the last-seen ids, the per-account waits and the recent
activity. No message bodies are stored, only counts, ids and verdicts.
