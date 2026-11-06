# Internal automation

The Automation tab can create recurring cycles that run **inside your own workspace with your own account**. Two of them are schedulable; the rest keep their consent surfaces.

## What can be scheduled

| Cycle | What it does | Bounds |
| --- | --- | --- |
| **Scrape members** | Reads the member list of one channel or group and stores the result on the schedule (`last_scrape_result`). Nothing is sent; no one is contacted. | A non-empty target (≤256 characters) and `scrape_limit` 1–1000. Bots are skipped by default. |
| **Auto join** | Joins the channels and groups you list, one at a time, with your own account. Invite links (`t.me/+…`) and public `@usernames` or `t.me/…` links are accepted. | 1–10 links per run and at least 35 seconds between joins. Already-member results count as skipped. |

The other internal actions remain on their own surfaces, because they contact other people:

- **Add members** opens the Adder, where each run needs its own consent confirmation.
- **Bulk send** opens the Bulk personal sender, where each recipient needs consent.
- **Auto reply** opens the rule editor, where keyword rules decide what is answered.

## Creating one

Automation → **Internal automation** → **Create** next to the cycle. The modal asks for:

1. A name.
2. The account that runs it (the first saved account is preselected).
3. The scrape target and limit, or the join links.
4. A repeat: **Once**, **Daily**, **Weekly** or **Weekdays**, plus an hour and minute.

The Automation tab also shows how many cycles of each kind are scheduled and lists the running cycles with their target. **Manage schedule** opens the full schedule list.

The same records are available over `POST /api/schedules` (fields: `scrape_target`, `scrape_limit`, `filter_bots`, `join_urls`, `join_delay`). A rejected request returns `400` and creates nothing.

## Validation

The same bounds are checked when the cycle is created and again every time it runs, because the stored schedule can be edited on disk. A schedule that no longer passes is disabled with `last_error = invalid_schedule_bounds_or_action` instead of running partly.

## Runtime behaviour

Both cycles run on the existing cron scheduler and therefore follow the same rules as every other schedule: one account per run, the result recorded on the schedule, and failures logged by type only — no Telegram response text or credential is written to the log.

Auto join stops the whole run on a Telegram flood wait and reports the seconds in the log; other per-target failures are counted and the remaining links are attempted at the configured spacing.
