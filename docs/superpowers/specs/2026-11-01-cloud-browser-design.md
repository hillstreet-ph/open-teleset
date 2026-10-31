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
| `GET/PUT /api/cloud-browser/config` | Read/save the DevTools address + access token. `PUT` with `clear: true` forgets them. All responses use the same public shape so the panel never loses its switch state. |
| `POST /api/cloud-browser/check` | `Browser.getVersion` + page list. |
| `GET /api/cloud-browser/pages` | Page targets only (iframes/workers filtered), capped at 40. |
| `POST /api/cloud-browser/pages` | `Target.createTarget` for an http(s) URL. |
| `DELETE /api/cloud-browser/pages/{id}` | `Target.closeTarget`. |
| `POST /api/cloud-browser/pages/{id}/navigate\|screenshot\|type\|key\|otp` | Per-page actions over a flattened session. |

Every action opens a connection, does one thing, and closes it. There is no session
registry, no reconnect loop and no background worker — the original draft promised one
and it was dropped as unnecessary: the panel is operator-driven, not scheduled.

## Boundaries that are enforced by construction

- **Telethon stays Telegram.** No Cloud Browser request model carries an `account_id`,
  and `cloud_browser.py` imports nothing from the Telegram client path. The only place a
  Telegram account appears is the OTP endpoint, which reads the encrypted recovery vault.
- **Write-only secrets.** The address and token are stored with the existing Fernet
  operator store and only the host is ever returned. `test_config_never_returns_the_address_or_token`
  locks this.
- **Text goes in, nothing comes back.** `type` returns a character count; `otp` returns
  seconds remaining. The generated code is typed on the server and never serialized.
- **No leak in errors.** CDP failures are collapsed into two fixed sentences. Vendor
  messages, addresses and tokens never reach an HTTP detail — the same contract the
  scraper fix established.
- **Dark by default.** `cloud_browser_enabled` starts `False` in `DEFAULT_FEATURES`; the
  panel and every endpoint 403 until the operator turns the switch on.
- Keys are limited to Enter/Tab/Escape/Backspace, so the panel cannot be used to hold a
  keyboard session.

## Testing

- `tests/test_cloud_browser.py` — 25 tests, with a CDP-shaped fake socket so the real
  client and real router run against each other (auth header forwarding, timeouts,
  refused commands, leak checks, OTP).
- A local harness (`dev_ui_server.py`, session workspace only) serves a fake DevTools
  WebSocket so the panel can be clicked through end to end. 23 button-level checks pass:
  save → check → pages → view → navigate → type → key → OTP → close → forget, plus a
  sweep of all 20 tabs with no unexpected console errors.
- `node --test tests/*.mjs` and the full pytest suite stay green.

## What this is not

Not a stealth browser, not a Telegram client, not a scheduler. If the operator wants the
dashboard itself to host the browser, that is a separate product decision about the
deployment image and instance lifecycle.
