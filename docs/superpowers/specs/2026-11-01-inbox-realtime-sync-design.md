# Inbox — realtime conversation wall

Date: 2026-11-01
Status: approved design, not yet implemented
Scope: `inbox.py` (new), `static/dashboard.html`, `operator_store.py`, `dashboard_features.py`, `dashboard.py`, `docs/INBOX.md`

## Problem

The operator runs several Telegram accounts. Today the dashboard can *act* on them —
send, schedule, scrape, auto-join — but it cannot *listen*. The only inbound surface is
Auto Reply, which polls each watched account every 30–900 seconds to find a private chat
worth answering. There is no place that shows what just arrived across all accounts, and
no way to tell at a glance which are people, which are groups, which are channels and
which are bots.

The request: an inbox like the official Telegram client, synced in real time, grouped by
chat type.

## Scope decided with the operator

**Read, and reply to what you opened.** Not a full mailbox workbench, not read-only.

- You see every chat on an account you opted in, newest first, with unread counts.
- You filter by bucket: personal, group, channel, bot.
- You open a chat, read its recent messages, and type a reply yourself.
- Nothing in the inbox sends on its own. Auto Reply's automation and the inbox are
  separate surfaces and neither imports the other.

Rejected: **full workbench** (mute, archive, folder management, history deletion) — a
large write-back surface against Telegram with no stated need. **Read-only monitor** —
rejected because an inbox you cannot answer from is a notification list, and you end up
switching clients anyway.

## What makes realtime possible here

Two facts read from the code, both load-bearing:

- `account_manager` keeps a **connected client per account** in `self.clients`
  (`account_manager.py:44`), handed out by `get_client()` (`:563`) with a shared task so
  concurrent callers reuse one connection. A Telethon client that stays connected
  receives Telegram's own update stream, so `events.NewMessage` can fire the moment a
  message lands.
- **Nothing registers event handlers today.** Auto Reply polls
  (`AutoReplyWorker.run`, `auto_reply.py:883`). This design adds the first push consumer.

The deployed container runs uvicorn with `--workers 1` (`deploy/Dockerfile:49`), so an
in-process cache and in-process fan-out are correct rather than merely convenient.

## Decision: push into an operator-scoped SSE stream

| Option | Verdict |
|--------|---------|
| **Telethon event handlers + per-operator SSE** (chosen) | True realtime, reuses the already-connected clients, and reuses the streaming idiom this product already ships and tests (`cloud_browser.py:775-807`: subscriber queues, heartbeat, `no-store`, `X-Accel-Buffering`). |
| Poll dialogs on a timer, like Auto Reply | No new connection surface, but 30–900 s behind and unread counts lag. That is a status page, not an inbox. Kept as the **Refresh now** fallback, not the mechanism. |
| Fetch only when the tab opens | Cheapest, zero background work — but the wall never moves by itself. |
| Reuse `/ws` | **Rejected on safety grounds.** `SupabaseAuthMiddleware` authenticates the socket (`security.py:175-236`) and the operator id is available in `scope["state"]["user"]`, but `ConnectionManager.broadcast()` (`dashboard.py:1099`) sends to **every** connected socket with no per-operator filtering. Pushing chat text through that path means a second browser tab or a second authorized operator's socket receives messages that are not theirs. Fixing it means re-plumbing the shared status broadcaster; SSE gets operator scoping for free because the route handler already knows its caller. |
| Ship Telegram Web in a frame | Rejected: it would put an unattended Telegram login inside the dashboard, which the Cloud Browser boundary already refuses to automate, and it duplicates Telethon with a second client. |

## Contract

All routes are `/api/inbox/*`, all refuse with **403 and one of our own sentences** while
`feature_enabled("inbox")` is false, following the Auto Reply gate idiom.

| Endpoint | Behaviour |
|----------|-----------|
| `GET /api/inbox/config` | Per-account opt-ins, stream state (`off`/`live`/`lost`/`cooldown`/`no-session`), watched count, and cache size. |
| `PUT /api/inbox/accounts/{account_id}` | `{enabled: bool}` — opt one account in or out, capped at `MAX_WATCHED`. Opting out detaches its handler. |
| `GET /api/inbox/chats?bucket=&account=&q=` | The wall from the in-process cache: newest first, capped at `MAX_CHATS`, each row carrying account, bucket, name, preview, timestamp, unread count. `q` matches **chat names only** within the cached wall — it is not a search of message history, which is out of scope. |
| `GET /api/inbox/chats/{ref}/messages?before=&limit=` | One page of a thread, `limit` ≤ `PAGE_MESSAGES`, read live from Telegram. |
| `POST /api/inbox/chats/{ref}/read` | Mark that chat read on Telegram and zero the local counter. |
| `POST /api/inbox/chats/{ref}/messages` | Send the text **you typed** — `text` ≤ `REPLY_MAX`. Refused when the peer is in the opt-out ledger. |
| `POST /api/inbox/refresh` | Snapshot dialogs for the opted-in accounts now. The non-realtime path, always available. |
| `GET /api/inbox/stream` | Server-sent events for this operator. Authenticated with the bearer; heartbeat every `HEARTBEAT_SECONDS`; `MAX_SUBSCRIBERS` per operator. |
| `DELETE /api/inbox/cache` | Clear the in-process wall for this operator. Never touches Telegram. |

`ref` is `hash_identifier(f"{operator}|{account_id}|{peer}")[:16]` — the same peer-ref
idiom as `auto_reply._peer_ref` (`auto_reply.py:247`, `PEER_REF_LENGTH = 16`). Raw peer ids
never reach the browser.

## Chat buckets

`_bucket(entity)` in `inbox.py`, from the Telethon entity type:

| Bucket | Rule |
|--------|------|
| `bot` | `User` with `bot` true |
| `personal` | `User`, not bot, not deleted |
| `group` | `Chat`, or `Channel` with `megagroup` |
| `channel` | `Channel` that broadcasts |
| `unknown` | anything else — excluded from the wall, and reported as `skipped_unknown` in `GET /api/inbox/config` so the operator can see chats are being passed over and why |

`auto_reply.py:251 _private()` proves the predicate works against the real library and
against the harness fakes. The inbox does **not** import it: `_private()` exists to answer
"may I auto-answer this", and the inbox answers a different question. Two small explicit
predicates beat one shared helper with two meanings.

## State: what is kept, and where

| Data | Where | Why |
|------|-------|-----|
| Message bodies, previews, thread pages | **Process memory only** (LRU per operator) | The product must not accumulate a disk corpus of other people's messages. Restart costs a re-fetch, which is the point. |
| Account opt-ins, unread counters, last-seen timestamps | Encrypted operator store (`write_secret("inbox", ...)`) | The badge and the opt-in list must survive a reload. |
| Handler registry, subscriber queues | Process memory | Ephemeral by nature. |

Bounds: `MAX_WATCHED = 8` accounts, `MAX_CHATS = 200` per operator, `MAX_SUBSCRIBERS = 4`,
`HEARTBEAT_SECONDS = 15`, `PAGE_MESSAGES = 50`, `STORED_MESSAGE_CHARS = 1200`,
`CHAT_PREVIEW_CHARS = 120`, `STALE_EVENT_SECONDS = 1800`, `REPLY_MAX = 700`,
`QUEUE_MAXSIZE = 200`.

Eight watched accounts rather than Auto Reply's twelve, because these are *persistent*
connections held open, not a burst every ninety seconds.

## Lifecycle

- **Attach**: when an opted-in account is first needed — the first stream subscribe or the
  first Refresh — `inbox.attach(operator, account_id)` calls `get_client()`, and registers
  one `events.NewMessage` handler per account, process-wide, tracked in a registry keyed by
  `account_id`.
- **Detach**: on opt-out, on the platform switch going off, and on process shutdown through
  the lifespan `finally` block (`dashboard.py:78-80`), alongside
  `auto_reply_worker.stop()`.
- **Nothing connects at boot.** No account is reached until the switch is on and the
  operator opted that account in.
- **Opting in means collecting while the tab is closed.** The handler lives server-side, so
  unread counters keep accruing when nobody is looking. This is stated in the UI in one
  line, because it is the kind of behaviour an operator should not discover later.

## Data flow

```
Telegram update ──▶ event handler ──▶ normalise ──▶ drop if older than STALE_EVENT_SECONDS
   │                                                        │
   │                                            upsert chat row + message page
   │                                                        │
   └── SSE route ──▶ subscriber queue (per operator) ◀── fan-out to that operator's queues
                            │
                            ▼
                     dashboard wall / thread
```

Stream payloads: `ready`, `chat`, `message`, `read`, `gone`, `state`, `ping`. A slow
subscriber whose queue is full is **dropped with a `state` event**, never allowed to stall
the handler — the handler must not await a browser.

## Error handling

| Failure | What the operator sees |
|---------|------------------------|
| Client unavailable | `state` event with a **fixed code** the inbox decides itself, never by parsing our own prose: before calling `get_client()` it checks `_paused_by_cooldown` → `cooldown`, a missing `session_string` on the account record → `no-session`, and a `saved_proxy` `ValueError` → `proxy-unavailable`; anything else that returns no client is `connection-unavailable`. Exception text never crosses the boundary (the scraper/`RPCError` contract). |
| Connection dies mid-session | `state: lost` and a **Refresh now** button. No automatic retry loop, so a banned-rate account is not hammered. |
| Telegram refuses the send | `HTTPException(400, "Telegram did not deliver that reply. Try again or answer in your own client.")` |
| Peer is opted out | `HTTPException(409, "That person asked not to be messaged. The reply is blocked.")` via `OutreachPolicy.is_suppressed` |
| Handler raises | `log.exception`, counters keep the last good value, the stream stays open. One bad chat must not close the wall. |
| Queue overflow | that subscriber is closed; the others and the handler are unaffected. |

## Safety boundary

The inbox **reads** conversations its own sessions are already members of, and **sends only
text a human typed at that moment**. It does not join groups, does not scrape, does not add
members, does not register accounts, does not answer on its own, and does not feed the
knowledge base. Auto Reply stays the only surface that sends without a human in the loop,
and it keeps its own gates.

## Testing

**Backend (`tests/test_inbox.py`)**, against a fake client exposing `add_event_handler`,
`remove_event_handler`, `iter_dialogs`, `get_messages`, `send_message`, `mark_read`:

1. Every bucket rule, including `megagroup`, broadcast and `deleted`.
2. An update older than `STALE_EVENT_SECONDS` never reaches a queue.
3. Opting an account out removes the handler exactly once.
4. The platform switch off ⇒ every route 403 with a reason.
5. `PUT /api/platform/features` round-trips `inbox_enabled` — the regression that made the
   Auto Reply switch persist nothing, locked from the start here (`DEFAULT_FEATURES` **and**
   `FeatureUpdate` must both declare the name).
6. Reply path: opt-out ledger blocks, length bound enforced, Telegram refusal returns our
   sentence and never the exception text.
7. LRU eviction at `MAX_CHATS`; queue overflow closes one subscriber and leaves the other
   receiving.
8. Nothing is written to disk except opt-ins and counters — assert the persisted shape has
   no message text.

**Dashboard (`node --test` source guard)**: inbox methods never reach
`/api/bulk`, `/api/schedules`, auto-join or adder endpoints.

**Real browser (new `ui_inbox.mjs` harness)**: the dev server stands a fake Telegram client
with a harness route (`/_harness/inbox/event`) that fires a normalised update into the real
handler, so the wall proves end to end: opting in an account, a new chat appearing without
a reload, bucket filters, opening a thread, marking read, typing and sending a reply,
"Clear this screen", and the stream closing when the tab is left.

## Out of scope, deliberately

Typing indicators, presence, reactions, media rendering, folders, search across all
history, end-to-end secret chats, and any multi-process cache. Each has its own reason to
exist and none is needed to make the wall live.

## Rollout

Ships **dark**: `inbox_enabled: False` in `DEFAULT_FEATURES`, switched on per deployment in
Integrations › Platform switches, then per account. No migration, no new infrastructure, no
new secret. Rollback is the switch — off detaches every handler and every route 403s again.
