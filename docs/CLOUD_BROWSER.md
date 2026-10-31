# Cloud Browser

Drives a hosted browser you already own — a vendor's DevTools address or your own
Chromium — for the web pages Telegram's MTProto accounts cannot reach: the Telegram Web
login flow, `my.telegram.org`, and the consoles behind your Cloud Phone numbers.

Telegram sends, joins, scraping and every account operation stay on the Telethon
accounts. The Cloud Browser never touches them.

## Turn it on

Integrations → Platform switches → **Cloud Browser**. It ships switched off, and every
endpoint answers `403` until it is on.

## Point it at your browser

1. Start (or rent) a browser that exposes its DevTools over a WebSocket, and copy the
   address, e.g. `wss://your-browser.example/session/abc?token=…`.
   ```bash
   chromium --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0
   ```
   Keep it on a private network or behind the vendor's token. Never expose an open
   DevTools port to the internet.
2. Paste the address and, if the vendor needs one, the access token into Accounts →
   Cloud Browser and press **Save address**.
3. **Check connection** shows the browser version and lists its open pages.

The address and token are encrypted per operator. The panel only ever shows the host, and
a blank token on a later save keeps the stored one. **Forget** deletes them.

## What you can do with a page

| Action | Result |
|---|---|
| Open / Refresh pages | Page targets only; capped at 40 |
| View | A still JPEG frame of the page |
| Go | Navigates the selected page to an http(s) URL |
| Send text | Types into the focused field, then clears the form |
| Enter / Tab / Escape / Backspace | The only keys the panel can send |
| Close | Closes the page in the browser |
| Send current code | Generates the Cloud Phone record's TOTP and types it into the page |

Each action opens its own DevTools connection and closes it afterwards. There is no
session held on the server.

## Privacy and consent

- Text you type and codes you send go straight into the browser. They are not stored, not
  repeated in any API response, and only counted in the activity log ("Typed 13 characters
  into a page").
- The generated code is typed server-side and never returned to the dashboard, so the only
  place it appears is the browser you are driving.
- No proxy rotation or fingerprint masking is built in. Automate only sites you are
  authorised to use, and stay inside Telegram's terms.

## Troubleshooting

| Message | Meaning |
|---|---|
| `Add your hosted browser address in the Cloud Browser panel first.` | Nothing saved for this operator yet. |
| `Could not reach the browser endpoint. Check the address and that it is running.` | The container could not open the WebSocket. Check the address, the port and any firewall between Zeabur and the browser. |
| `The browser endpoint refused the DevTools command.` | The browser is reachable but does not accept that command; a vendor may restrict parts of DevTools. |
| `SESSION_ENCRYPTION_KEY is not configured…` | No key on the server, so nothing can be stored. Set `SESSION_ENCRYPTION_KEY`. |
| `Cloud Browser is switched off…` | Enable it under Platform switches. |

Design decisions and the endpoint contract live in
[docs/superpowers/specs/2026-11-01-cloud-browser-design.md](superpowers/specs/2026-11-01-cloud-browser-design.md).
