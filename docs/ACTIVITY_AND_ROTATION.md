# Activity filters and sender rotation

Scraper and Bulk Personal Sender support `all` (default), `recently`, `7_days`,
`15_days`, and `30_days`. Select the activity window in either dashboard tab.

| Telegram-visible status | Recently | 7 days | 15 days | 30 days |
|---|---|---|---|---|
| Online / recently | Included | Included | Included | Included |
| Within a week | Excluded | Included | Included | Included |
| Within a month | Excluded | Excluded | Excluded | Included |
| Hidden / unknown | Excluded | Excluded | Excluded | Excluded |

Exact offline timestamps use an inclusive UTC cutoff; recently means up to
three days. Approximate statuses remain approximate. No hidden timestamp is
inferred or privacy setting changed. See [Telegram last-seen documentation](https://telegram.org/faq#q-can-i-hide-my-last-seen-time).

The scraper scan limit is 1–10,000 participants before bot/activity filtering.
The response shows scanned, excluded and matched counts, plus visible activity.
An empty filtered result is valid and is not a scraping failure.

Bulk Sender checks each explicitly entered recipient through its assigned
account immediately before dispatch. Activity is an additional filter, not
consent. Scraped membership is never treated as consent or automatically copied
to the sender. The existing server-owned approval, explicit consent,
suppression and RPC-boundary checks remain required.

## Account selection

Single-account requests retain `account_id`. For automatic round-robin rotation,
use `rotation_mode: "round_robin"`, `account_ids` containing 1–5 distinct account
IDs, and an empty `account_id`. The dashboard sends these fields automatically.
All selected accounts must be available before the first message is attempted.
Approvals must cover the pool; consent must cover each recipient's assigned
sender. No alternate sender is tried when permission or delivery fails.

`per_account_limit` is a per-run cap from 1–20, default 20. It does not increase
the existing maximum of 20 entered recipients across the whole request.
Requests exceeding pool capacity are rejected before connecting accounts.
Rotation starts at the first selected account on each run; it is not a daily
quota or a persistent campaign scheduler. Duplicate entries are normalized,
and resolved Telegram peer IDs are deduplicated across the whole run.

The 3–3,600 second delay applies between attempts across accounts, as well as
the existing shared per-account rate limiter. Activity-excluded recipients are
reported as skipped. Every attempted recipient shows its assigned account and
outcome. Interrupted batches show pending recipients; they are not silently
resumed or automatically resent.

## Provider restrictions and recovery

A Telegram flood wait stops the entire batch and stores the reported cooldown.
Peer-flood, frozen, banned or invalid-session errors stop the batch and impose
a 24-hour cooldown, requiring operator investigation before another run.
Policy or rate-store denial also stops the batch. Other recipients are never
rerouted to evade a restriction. Unknown delivery failures are not retried.

Cooldowns share the existing mounted `OUTREACH_RATE_DB` SQLite file. All workers
for an account must use that same persistent path. A later worker cannot shorten
a cooldown, and a cooldown created while an attempt is waiting blocks dispatch.
An unavailable rate store fails closed. The dispatcher applies cooldown checks
to all existing outreach callers, including batch and scheduled operations.

Tests use synthetic peers and mocked clients; no real Telegram messages are
sent. Deployment acceptance still requires a healthy runtime database and
authenticated UI/API checks. Roll back code by the previous verified commit;
retain the rate database, encrypted sessions and volume when rolling back.

## CSV and bot controls

All three tabs default to excluding bots. Sender and Adder resolve each recipient
before filtering; exclusions show as skipped and never dispatch an RPC. Adder
also supports the same activity windows and stops on provider restrictions.
Numeric recipient IDs resolve as integers, preventing phone-contact lookup.

Sender CSV headers: `recipient` (or `username`, `target`, `id`) and optional
`message`. Messages can differ by recipient; missing row messages use the
message box. A one-row, message-only CSV fills the message box. Import only
prepares a reviewed batch; it does not send, record consent or create approvals.
The current 20-recipient limit and all existing policy checks remain. Duplicate
recipients with conflicting messages are rejected. Editing the recipient field
clears imported individual messages. Adder CSV supports up to 10 recipients.

Scraper Export CSV contains username, ID, names, bot flag and visible activity.
It preserves quoted/multiline values and protects spreadsheet formula cells.
Exported IDs are a fallback when the username is empty. A scraper export is
not proof of consent. Files are parsed in the browser, capped at 2 MB, and are
not uploaded or saved by import. Blank or malformed files show an error without
replacing the current batch.

## English built-in templates

At startup, unchanged Chinese greeting/notification names and text are
translated to English. IDs, variables and usage history are retained. Custom
names and custom message text are preserved. The greetings use `{name}` and
`{time}`; notification uses `{content}`, `{date}` and `{time}`.

## Free global proxy default

When no global proxy exists, background startup discovery checks up to 12
SOCKS5 candidates from Proxifly's public list. Only public IP addresses with
valid ports are accepted; private addresses, hostnames and credential-bearing
entries are rejected. Candidates must pass Telegram TCP tunnel and certificate-
verified Telegram website TLS checks. No account credentials or Telegram RPCs
are used in testing. Selection is bounded, not a guarantee of availability.
Source: https://github.com/proxifly/free-proxy-list

One verified candidate becomes the persisted global default; per-account
assignments take priority. Existing global settings are preserved. Accounts
already connected must reconnect to apply a changed proxy. There is no automatic
proxy rotation after a Telegram restriction. Removing the global proxy disables
future discovery persistently. Find free global proxy explicitly re-enables it.
If the source or candidates fail, no proxy is saved; logs show the outcome.
Liveness and sign-in startup never wait for discovery. Public proxy availability
and performance can change; the connected status is not delivery verification.

Rollback to the preceding commit retains encrypted sessions, proxy configuration
and the rate database. No database migration is required for these additions.
