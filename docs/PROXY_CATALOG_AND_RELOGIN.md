# Free MTProto proxy catalog and in-place re-login

Two operator surfaces share one rule: nothing is changed on an account or a connection until the operator picks it and the product can prove the choice works.

## Free MTProto proxy catalog

Proxies → **Free MTProto proxies** reads three fixed public lists (0xRadikal Free-Telegram-Proxy, StormyCloud MTProto, vanced.to/telegram) and shows every endpoint as a card. Only `tg://proxy` and `t.me/proxy` links with a global host, a port in 1–65535 and a usable secret become cards. Duplicates are merged; a source that fails or serves nothing is reported on its own line and never fails the rest of the list.

`GET /api/proxies/catalog` caches the merged list for ten minutes and attaches a TCP reachability result and latency to each card (cached for two minutes, ten probes at a time). An unreachable card is labelled and its **Connect** button is disabled; it can still be retried with **Verify**.

**Verify** is a real Telegram handshake: `POST /api/proxies/catalog/verify` opens a MTProto client with the endpoint's secret and reports success or a named failure. A TCP connect alone is not treated as proof.

**Save** (`POST /api/proxies/catalog/save`) stores the endpoint as a proxy whose ID is either supplied by the operator (1–60 letters, digits, `_`, `-`) or derived from a truncated hash of host, port and secret. The same call can additionally set it as the global proxy or assign it to one account and switch that account to the selected proxy.

MTProto secrets are normalised before use: 32 hex digits gain the `dd` tag, 16 raw bytes are accepted as hex or base64, and `ee` fake-TLS secrets are preserved byte-for-byte. Invalid secrets are refused with a 400. Secrets are never written to logs or returned to the page.

Telegram clients only use the obfuscated MTProxy transport when the connection class is a `TcpMTProxy` subclass, so every client built from a `(host, port, secret)` tuple also receives `ConnectionTcpMTProxyRandomizedIntermediate`. A free proxy is public infrastructure: it can disappear, be slow, or see metadata. It is not a rate-limit or ban guarantee.

## Re-login a logged-out account

A saved account whose session expired, was revoked, or is in a Telegram cooldown keeps the row it already has: its account ID, usage count, proxy assignment and settings. The Accounts list shows **Re-login** for any account whose last connection error reads `session expired or revoked`, `requires a wait of` or `different data center`.

Re-login runs the normal QR or phone sign-in through a temporary account ID and passes `replace_account_id` to `/api/accounts/generate-qr` or `/api/accounts/send-code`. The backend refuses an unknown replacement ID with a 404 before any Telegram client is built.

The fresh authorization replaces the saved account only after Telegram reports the same user ID as the account being restored. A different Telegram user is refused with "the account is not the one being re-signed in" and nothing changes. On success the session string, phone, username, name and premium flag are refreshed, the usage count, proxy assignment and creation time are kept, the connection error is cleared and a stale client for that account is disconnected. The success toast says the account was restored rather than that a new account signed in.

Phone re-login prefills the saved number split into country code and national part, so the operator confirms instead of retyping. QR re-login can start immediately; phone-code sign-in is still subject to Telegram's cooldown, and the modal states the remaining wait in minutes. Cooldown text comes from Telegram; follow its duration and use the official appeal process for incorrect restrictions.
