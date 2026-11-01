# Cloud Browser — design

Date: 2026-11-01
Status: implemented
Scope: `cloud_browser.py`, `src/open_teleset/cdp_client.py`, Cloud Browser panel in `static/dashboard.html`

## Problem

The operator automates sites that are not reachable through Telethon — the Telegram Web
login flow, `my.telegram.org`, and other web consoles their Cloud Phone numbers own.
They already run a browser somewhere (a vendor's hosted Chromium or their own instance)
and want the dashboard to drive it.

Two constraints from the product owner shaped every decision below:

- "dont replace for official telegram Telethon MTProto" — the Cloud Browser is **not** a
  second Telegram client. Sends, joins, scraping and account operations stay on Telethon.
- "No proxy rotation or fingerprint evasion is built in."

## Decision: speak CDP to a browser the operator already owns

The dashboard connects to a DevTools WebSocket address the operator supplies and speaks
Chrome DevTools Protocol over it (`src/open_teleset/cdp_client.py`).

Rejected alternatives:

| Option | Why rejected |
|--------|--------------|
| Ship Playwright/Chromium inside the deployed image | `deploy/requirements-prod.txt` has no Playwright and the Zeabur container is a slim FastAPI image; adding a browser makes the image heavy and the instance stateful. `web_login.py` already proved the local-Playwright route and it does not survive this deployment model. |
| Vendor SDK (Browserless/Anchor/Steel) | Ties the feature to one provider's terms and pricing, and every vendor change would land in the product. CDP is the common denominator they all expose. |
| Reuse the n8n/HeroSMS automation path | n8n owns the SMS credentials; routing browser control through it would put the operator's browser token behind a second system. |

Choosing CDP means the product owns one thin transport and the operator owns the browser,
its fingerprint, its proxies and its lifetime. That is the boundary we want.

## Contract

| Endpoint | Behaviour |
|----------|-----------|
| `GET/PUT /api/cloud-browser/config` | Read/save the DevTools address + access token **of the browser in use**. `PUT` with `clear: true` forgets every browser. All responses use the same public shape so the panel never loses its switch state. |
| `POST /api/cloud-browser/profiles` | Add a named browser from an address + token + label, switch to it and drop the live view. 409 past the cap. |
| `PUT /api/cloud-browser/profiles/{id}` | Edit one browser: blank token or label keeps what is stored. Tears down the live view only if that browser was being viewed. |
| `POST /api/cloud-browser/profiles/{id}/activate` | Make that browser the one every later action talks to. |
| `DELETE /api/cloud-browser/profiles/{id}` | Forget one browser; the most recently used survivor becomes active, and forgetting the last empties the record. |
| `POST /api/cloud-browser/check` | `Browser.getVersion` + page list. |
| `GET /api/cloud-browser/pages` | Page targets only (iframes/workers filtered), capped at 40. |
| `POST /api/cloud-browser/pages` | `Target.createTarget` for an http(s) URL. |
| `DELETE /api/cloud-browser/pages/{id}` | `Target.closeTarget`. |
| `POST /api/cloud-browser/pages/{id}/navigate\|screenshot\|type\|key\|otp` | Per-page actions over a flattened session. |
| `GET /api/cloud-browser/pages/{id}/stream` | Frames as server-sent events while the live view is open. Authenticated with the operator's bearer token. |
| `POST /api/cloud-browser/pages/{id}/mouse` | Click, double-click, right-click, move and wheel at a point expressed as a fraction of the picture. |
| `POST /api/cloud-browser/pages/{id}/reel` | Start recording frames into the live session. |
| `GET /api/cloud-browser/pages/{id}/reel` | The reel as a ZIP of numbered JPEGs, in paint order. |
| `DELETE /api/cloud-browser/pages/{id}/reel` | Discard the reel. |
| `POST /api/cloud-browser/live/close` | Stop the view and release the connection. |

With no live view open, every action opens a connection, does one thing, and closes it —
no reconnect loop, no background worker. The original draft promised a session registry
for scheduled browsing and it was dropped as unnecessary: the panel is operator-driven.

The one session the server does hold is the live view. It exists because Chrome stops
painting until the newest frame is acknowledged, which needs a reader that stays open and
answers each frame — a request/response call per click cannot do that. It is scoped to the
operator who opened it, shared by up to four watchers of the same page, and closed when the
last viewer leaves, the page closes, a new address is saved, the operator switches to
another named browser, or a minute passes with nobody watching. Commands issued while it is
open ride that connection instead of reconnecting.

## Live view: three decisions worth recording

**Frames over an authenticated event stream, not a WebSocket.** `EventSource` cannot set a
header, so the panel reads the stream with `fetch` and a `ReadableStream` and attaches the
same Supabase bearer every other call uses. The Cloudflare Worker passes the response body
through unbuffered, and a 15-second ping keeps an idle page from being treated as a dead
connection by the Worker or the container. A WebSocket would have needed a second auth
path; `/ws` already has one, but frames-only over a socket buys nothing here.

**Coordinates are fractions of the picture.** The obvious implementation maps a click by
the JPEG's pixel size, which breaks the moment the browser window is resized, the page is
zoomed, or the panel is a different width than the image. Instead the panel reports where
on the *picture* the operator pointed (0..1) and the server multiplies it by the browser's
own `cssVisualViewport`. The browser decides where the pointer lands; the dashboard never
guesses the page size and there is no letterbox math.

**Keyboard comes from the server.** The panel does not hard-code its key buttons — it
renders `config.keys`, the same allowlist the endpoint validates against. A key that
appears is a key that works, and narrowing the allowlist is a one-line change on one side.

## Named browsers: the decisions worth recording

A login flow, a vendor console and `my.telegram.org` should not share one browser, so an
operator keeps up to 12 named connections. Three choices shaped the storage:

**One record holds every browser.** Add, switch, edit and forget each rewrite the whole
profile map in a single encrypted write, so an interrupted change cannot leave two browsers
claiming to be active or an `active` id pointing at a browser that was just forgotten.

**The active browser is derived, not trusted.** A forgotten browser can leave a stale `active`
id behind (a second tab, or a hand-edited vault). Rather than error, the read falls back to
the most recently updated profile, so the panel always talks to something that exists.

**The legacy address migrates lazily.** An operator who saved before named browsers existed
reads back as one browser keyed `default` (named after its host, like any browser saved
without a label), and the first write drops the old top-level copy of the secret. Rejected:
a boot-time pass over every operator's vault to rewrite the shape — that decrypts the whole
store to fix one field, and this deployment runs with a single worker that would do it
before serving a request.

**Switching browsers drops the live view.** Frames come from the socket opened against one
address. Leaving the cockpit running while the panel says "this browser is in use" would show
one browser and drive another, so adding a browser and switching both tear the session down;
forgetting a browser you are not viewing leaves it alone.

**A profile is an address, not a session.** Nothing is copied between browsers: their cookies
and logins live in the browser each address points at, which is exactly the separation the
operator is paying the vendor for.

## Boundaries that are enforced by construction

- **Telethon stays Telegram.** No Cloud Browser request model carries an `account_id`,
  and `cloud_browser.py` imports nothing from the Telegram client path. The only place a
  Telegram account appears is the OTP endpoint, which reads the encrypted recovery vault.
- **Write-only secrets.** The address and token are stored with the existing Fernet
  operator store and only the host is ever returned — in the config and in the named-browser
  list, which adds a label and a "a token is stored" flag but no address.
  `test_config_never_returns_the_address_or_token`,
  `test_a_named_browser_is_listed_by_label_and_host` and
  `test_the_browsers_address_never_reaches_the_viewer` lock this.
- **Text goes in, nothing comes back.** `type` returns a character count; `otp` returns
  seconds remaining. The generated code is typed on the server and never serialized.
- **Frames are transient.** Screencast frames are streamed to the operator and written
  nowhere — not to disk, not to a storage bucket, not to the database. The reel is a list
  in the live session's memory, bounded at 300 frames and 8 MB, and is gone when the view
  closes. The only file produced is the ZIP the operator downloads to their own machine.
- **No leak in errors.** CDP failures are collapsed into two fixed sentences. Vendor
  messages, addresses and tokens never reach an HTTP detail — the same contract the
  scraper fix established.
- **Dark by default.** `cloud_browser_enabled` starts `False` in `DEFAULT_FEATURES`; the
  panel and every endpoint 403 until the operator turns the switch on.
- Keys are limited to the 14 the client can press without holding a keyboard session
  (Enter, Tab, Escape, Backspace, Delete, Space, Home, End, PageUp, PageDown, four
  arrows). The server returns that list and the panel renders it, so the UI cannot offer a
  key the endpoint would reject.

## Testing

- `tests/test_cloud_browser.py` — 70 tests, with a CDP-shaped fake socket so the real
  client and real router run against each other. Beyond the original surface (auth header
  forwarding, timeouts, refused commands, leak checks, OTP), the live layer is covered
  frame by frame: every frame is acked, frames from a session we do not own are ignored and
  never acked, a slow watcher cannot grow the queue, a view nobody watches is released,
  commands ride the open connection rather than reconnecting, switching pages tears down
  the previous stream, saving a new address drops the session, the browser's address never
  appears in the state the viewer polls, clicks map onto the browser's own viewport, an
  out-of-range or unsupported pointer action is refused before connecting, recording requires
  the live view, and the reel download name is sanitized. The named-browser layer covers the
  storage shape end to end: a list shows labels and hosts and nothing else, an added browser
  becomes the one commands use, switching talks to the other address and releases the live
  view, using the browser already in use keeps it, editing keeps a blank token, an edit
  touches only that browser, forgetting keeps the others and the last forget empties the
  store, the cap answers 409, an unknown id 404, a spaces-only label falls back to the host,
  a legacy record reads as one browser and loses its top-level secret on the next write, and
  another operator's record is not readable.
- A local harness (`dev_ui_server.py`, session workspace only) serves a fake DevTools
  WebSocket that actually paints: it renders the frame counter, the typed text, the last
  key, click rings and a scroll offset, so a click that reached the browser is visible in
  the picture that comes back. It now serves two of them under separate addresses, each
  answering with its own version string, its own page list, its own picture header and its
  own connection log, so a browser switch is proved by what the browser reports rather than
  by what the DOM says. 28 button-level checks pass on the cockpit — the original save →
  check → pages → view → navigate → type → key → OTP → close → forget sweep, plus start live
  view → frames counted → click → wheel → keys rendered from the server list → record →
  download the reel as a valid ZIP → switch pages without losing the view → stop, with no
  unexpected console or server errors. A further 21 pass on named browsers: save and connect,
  the browser answers under its own name, an empty address adds nothing, two browsers on
  record, the new one is in use and its pages are listed, switching re-reads the version and
  opens a connection to that address, forgetting one leaves the survivor active, forgetting
  all leaves the panel asking for setup, and no stored address is ever shown as more than a
  host.
- `node --test tests/*.mjs` and the full pytest suite stay green (634 passed).

## What this is not

Not a stealth browser, not a Telegram client, not a scheduler. If the operator wants the
dashboard itself to host the browser, that is a separate product decision about the
deployment image and instance lifecycle.
