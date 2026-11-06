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
you read. A chat whose last message is a file gets a line for that file — `Photo · 101 B`,
`report.pdf · 240 KB` — instead of a blank row.

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

## Files and photos

A message that carries something besides text is described, not shipped. Every thread row and
every chat preview gets a small descriptor:

| Field | What the browser is told |
| --- | --- |
| `kind` | which Telegram attachment it is: `photo`, `sticker`, `voice`, `video_note`, `gif`, `video`, `audio`, `document` |
| `label` | the word the operator reads: Photo, Sticker, Voice message, File |
| `name` | the file name Telegram attached, if the message has one |
| `mime` | the declared content type; a photo with no type is treated as `image/jpeg` |
| `size` / `human` | the byte count, and the same number already formatted |
| `inline` | whether this screen renders it as a picture |

The `file_id` and `access_hash` never leave the process. They are re-resolved server-side, inside
the chat the reference already points at, so guessing an id from another conversation gets nothing.
`document` is matched last on purpose: Telegram sets it for gifs, videos and voice notes too, and a
descriptor that matched it first would label all of those as a plain file.

`inline` is decided by the server, from `image/jpeg`, `image/png`, `image/gif` and `image/webp`,
so the thread that renders a picture and the route that serves it cannot drift apart. Anything
else arrives as a chip with its name, its size and **Save file**.

The bytes come from `GET /api/inbox/media/{ref}/{message_id}`, stream straight out of Telegram into
the response, and are never written to disk. Each one carries `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff` and a `Content-Disposition` built from a sanitized name, because
Telegram file names arrive with paths, quotes and semicolons in them. The declared size is checked
before anything moves, and the 8 MB cap is enforced again as chunks arrive for a sender that
declared nothing. What the thread shows is the server's own sentence, never a blank box:

| Response | When |
| --- | --- |
| 404 — "That message has no file to open here." | a text message, or an id this chat does not have |
| 413 — "That file is bigger than the 8 MB this screen will carry." | over the cap |
| 400 — "Telegram did not hand back that file. Use Refresh now and try again." | Telegram refused |

The page asks for these bytes with its authenticated client and renders a blob URL rather than
pointing an `<img src>` at the route. Authentication on this product is a Bearer header and tokens
in URLs are not supported, so an image tag could never pass the check — and a token in a URL is a
credential in browser history, server logs and every proxy in between.

Screens are bounded on this side too. Opening a chat fetches at most the 12 newest inline
attachments, one at a time, and stops if you move to another chat mid-fetch. Switching chats or
pressing **Clear this screen** revokes every blob the browser was holding.

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
now** is for. Attachments follow the same rule: a file is fetched when you ask for it and
streams past the server, so nothing of theirs is kept here either.

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
| One attachment | 8 MB, streamed and never written to disk |
| Kinds rendered as a picture | jpeg, png, gif, webp |
| Pictures fetched when a chat opens | 12 newest, one at a time |

These are deliberate. Eight accounts is already a wide surface to be watching, and Telegram
does not reward an aggressive one.
