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
