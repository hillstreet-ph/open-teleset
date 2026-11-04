# Compact record lists

Every list in the dashboard that can grow without bound is rendered through one of two
components. Neither scrolls forever: the panel shows a short, readable head and offers the
rest behind one button, so a year of job history costs the same screen space as a week's.

## `<record-list>` — collapsed rows

For records with detail behind them: outreach jobs, progress rows, Copilot history and the
preset runbook. Each record is a native `<details>` row, so it is keyboard-operable and
collapses without JavaScript state.

```html
<record-list :items="outreachJobs" :key-of="job => job.id"
             :label="job => 'Message send · ' + job.id.slice(0, 8)"
             :state="jobStateLabel" :state-class="jobStateBadge"
             :open-when="jobIsActive" :limit="5"
             empty="No outreach jobs yet.">
```

- `label` is a one-line summary. The convention is `What it is · short id`, which reads as
  `Message send · 623697d4` or `Auto Join · dc1cc880`.
- `state` and `state-class` render the status badge in the summary line, so the state is
  visible without opening the row.
- `limit` is how many rows are shown before the **Show all (N)** button.
- `open-when` keeps a row expanded — a running job stays open past the limit, because
  collapsing it would hide the progress bar you are watching. Finished rows collapse.
- `empty` is the copy for an empty panel. It names the action that fills it.

## `<bounded-list>` — a slice of anything

For lists that already render their own rows: chats, contacts, accounts, rules, proxies,
logs, knowledge notes, browser profiles. It is a `display: contents` wrapper, so the child
markup keeps the grid and flex layout it had before and only the count changes.

```html
<bounded-list :items="mailbox.chats" :limit="12">
    <template #default="{ items: shownChats }">
        <button v-for="chat in shownChats" :key="chat.ref" class="proxy-item">…</button>
    </template>
</bounded-list>
```

The slot receives the visible slice, never the whole array. Below it, **Show all (N)** /
**Show fewer**.

## Tables

Real `<table>`s cannot take a `display: contents` wrapper, so they use the same rule through
`visibleRows(items, key, limit)` and `toggleRows(key)`, with the button in a full-width cell.

## Current limits

| Panel | Component | Rows |
| --- | --- | --- |
| Automation runs (both places) | table | 8 / 12 |
| Outreach jobs | record-list | 5 |
| Webhooks, integrations, API keys, sessions, proxies, profiles, pages | bounded-list | 8 |
| Contacts, inbox chats | bounded-list | 12 |
| Cloud Phone accounts | bounded-list | 10 |
| Logs | bounded-list | 20 |
| Copilot history | record-list | 8 (component default) |
| Copilot presets | record-list | 10 |
| Accounts, Auto Reply rules and conversations | bounded-list | 8 |
| Auto Reply outbox | bounded-list | 5 |
| Knowledge documents and matches | bounded-list | 10 |
| Repeated wording suggestions | bounded-list | 8 |
| Schedules | bounded-list | 6 / 10 |
| BulkSend results | bounded-list | 25 |
| Progress jobs | record-list | 5 |

When you add a panel that lists records, use one of these rather than a `v-for` over the
whole array, and do not put a fixed-height scroll box around it — the bounded head is the
mechanism, and a nested scrollbar is what this replaced.
