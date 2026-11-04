# Inbox: your chats, moving as they arrive

The Inbox is the product's first push consumer. Telegram already sends updates to the
clients `account_manager` keeps open, so the inbox reads those updates instead of asking
for them again. It looks like Telegram's own chat list: one row per chat, filed by what the
chat actually is, with unread counts that clear when you open it.

It ships switched off. Turn it on under **Integrations › Platform switches** (the switch is
called Inbox). While it is off every route under `/api/inbox/*` answers 403 with
"Inbox is switched off. Turn it on under Integrations › Platform switches." and the panel
says the same thing rather than showing an empty wall.

## What it shows

Open **Inbox** and press **Refresh now**. The wall is built from the dialogs each watched
account actually has, and every chat is filed into one of four buckets from what Telegram
reports about the peer:

| Bucket | Filled from |
| --- | --- |
| Personal | a plain user you can talk to |
| Bot | a user account flagged as a bot |
| Groups | small chats and Telegram supergroups |
| Channels | broadcast channels |

Anything Telegram describes in another way is not invented into a bucket: the panel reports
how many chats could not be classified instead of filing them somewhere plausible.

Each row carries the name, the bucket, the account it belongs to, the last preview and an
unread pill. The sidebar badge on **Inbox** is the sum of those unreads, so it goes down as
you read.

Filters are local to the screen: the account selector, the four bucket buttons and the search
box narrow the same wall without another round trip.

## Realtime, not polling

While you are on the tab it holds one Server-Sent-Events stream (`/api/inbox/stream`). The
stream opens only after the config has answered, so it never starts against a closed inbox.
It sends the current wall first, then every update Telegram pushes, then a heartbeat every
15 seconds to keep the connection warm. A pushed message repaints the row, the unread pill
and the sidebar badge without a refresh.

Four tabs at most can hold the stream. A fifth is refused with "Four tabs already have this
inbox open." A tab that stops keeping up is dropped from the fan-out and reports `slowed` —
reload it and it catches up from the wall again.

Leaving the tab closes the stream. Nothing keeps running in the background on your behalf.

## Replying

The one write path in this subsystem is a reply you type into the open chat and send. It
goes out through the same Telethon session as any message you send yourself, is capped at
700 characters, and is refused with HTTP 409 if that person is on the opt-out ledger:
"That person asked not to be messaged. The reply is blocked."

The ledger is read before every send, and the read failing closed is also a 409: "The opt-out
list could not be read, so nothing was sent. Check the outreach policy file." A reply is never
sent on the assumption that nobody opted out.

Nothing here sends by itself. An incoming message does not trigger a reply, an outgoing
event does not count as a send, and the inbox has no rules engine — that is Auto Reply, a
separate feature with its own switch and its own limits. A log line is only written when an
actual send happens ("Sent a reply you typed").

Opening a chat marks it read on Telegram, not just on screen. If Telegram refuses the mark,
the counter still clears locally and the failure is logged without blaming you for it.

## States

Each watched account reports why it is or is not attached, and the panel shows the label
rather than a bare error:

| State | Meaning |
| --- | --- |
| Live | Watching this account for new messages. |
| Off | This account is not opted in. |
| No session | No saved Telegram session. Sign in again to watch it. |
| No account | This account no longer exists here. |
| Cooldown | Telegram is asking this account to wait before it connects again. |
| Proxy unavailable | The saved proxy for this account is unavailable. |
| Connection unavailable | Telegram did not hand back a connection. Use Refresh now. |
| Lost | The connection stopped. Use Refresh now to start it again. |
| Slowed | This tab stopped keeping up. Reload it to catch up. |

An account in Cooldown or Proxy unavailable stays opted in; the inbox simply does not
pretend to be watching it.

## What is stored, and what is not

Only your own settings are persisted. The encrypted store sees the list of opted-in accounts
and the unread counters — nothing else. Message bodies, sender names and chat previews live
in this process's memory and are gone when the container restarts, so no corpus of other
people's messages accumulates on disk. A restart costs a re-read, which is what **Refresh
now** is for.

Other people's chats are addressed by an opaque reference: the account id and peer id are
hashed with your operator id and truncated to 16 characters. The API never returns the raw
peer or the username of a chat, and the wall holds at most 200 chats, 50 messages per open
chat and 1,200 characters per message. Updates older than 30 minutes are dropped rather than
pushed into the wall as if they had just happened.

**Clear this screen** empties the wall for your operator without contacting Telegram and
without touching the opt-ins. Turning the platform switch off detaches every handler and
stops every stream immediately.

## Bounds

| Setting | Limit |
| --- | --- |
| Watched accounts | up to 8, and you opt in per account |
| Chats on the wall | 200 |
| Messages kept for an open chat | 50 |
| Stored characters per message | 1,200 |
| Reply length | 700 |
| Simultaneous stream tabs | 4 |
| Stale events | dropped after 30 minutes |

These are deliberate. Eight accounts is already a wide surface to be watching, and Telegram
does not reward an aggressive one.
