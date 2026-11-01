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
2. Paste the address, a name to tell it apart by, and — if the vendor needs one — the access
   token into Accounts → Cloud Browser and press **Save address**.
3. **Check connection** shows the browser version and lists its open pages.

The address and token are encrypted per operator. The panel only ever shows the host, and
a blank token on a later save keeps the stored one. **Forget all** deletes every browser.

## Keep more than one browser

A login flow, a Cloud Phone console and a `my.telegram.org` session do not belong in the
same browser, so one operator can name up to 12 of them. Each is a separate connection with
its own address, token and cookies — signing in on one does not sign you in on another.

| Button | Result |
|---|---|
| **Add a named browser** | Stores the address as a new browser and switches to it. The live view closes, because it was showing the other browser's page. |
| **Use this browser** | Switches every later action to that address and re-reads its version. Your page list changes with it. |
| **Forget** (on one row) | Removes that browser only. If it was the one in use, the most recently used survivor takes over. |
| **Save address** | Edits the browser in use: the name and token fields may be left blank to keep what is stored. |

The row for the browser in use carries an **In use** badge, and the **In use** status line
repeats the name so you can see whose page you are looking at before you click. An address
saved before named browsers existed reads back as a single browser named after its host, so
nothing is lost on the upgrade.

## What you can do with a page

| Action | Result |
|---|---|
| Open / Refresh pages | Page targets only; capped at 40 |
| View | A still JPEG frame of the page |
| Go | Navigates the selected page to an http(s) URL |
| Send text | Types into the focused field, then clears the form |
| Keys | The 14 keys the browser accepts: Enter, Tab, Escape, Backspace, Delete, Space, Home, End, PageUp, PageDown and the four arrows. The panel renders exactly this list, so a key you can see is a key that works. |
| Close | Closes the page in the browser |
| Send current code | Generates the Cloud Phone record's TOTP and types it into the page |

## The live cockpit

**Start live view** watches the selected page as it moves. The container asks the browser
to paint frames (`Page.startScreencast`), and each one reaches the panel over an
authenticated event stream — the same Supabase bearer as every other call, read with
`fetch` because `EventSource` cannot carry a header.

While the view is live the panel is a pointer:

| Input | What the browser sees |
|---|---|
| Click / double-click | A left press at that point, twice for a double |
| Right-click | A right press |
| Wheel | A scroll by that delta |
| Move | The pointer position, without pressing |

The panel sends where on the *picture* you pointed (a fraction from 0 to 1), and the
browser multiplies it by its own viewport. A resized window, a zoomed page or a different
screen density does not move your click.

Frames arrive about twice a second. A viewer that falls behind sees the newest frame rather
than a backlog, so a slow laptop or a throttled tab cannot make the queue grow. Up to four
people can watch one page, and a view nobody watches for a minute closes itself.

**Record frames** keeps each frame in memory while the view is live — up to 300 frames or
8 MB, whichever comes first — and **Download reel** gives them back as a ZIP of numbered
JPEGs in the order they were painted. Nothing is written to disk or to a storage bucket on
the server; the reel lives and dies with the view.

Commands ride the connection the cockpit already opened, so clicking does not reconnect for
every press. Switching pages keeps the view open on the new page; saving a new address,
switching to another named browser or closing the page drops it, and leaving the tab stops
the stream.

Each action with no live view open uses its own DevTools connection and closes it
afterwards. The only session the server holds is the one a live view opened.

## Let your own AI plan the steps

With a page selected, the **AI plan** card asks the provider you configured in Settings → AI
providers to read that page and propose a route through it. This is a drafter, not a driver:
nothing runs until you press that step's button, steps run in order, and a fifth proposal drops
the oldest plan.

| Button | Result |
|---|---|
| **Propose steps** | Reads the page's controls, asks your provider for up to 12 steps, and lists them with each one's reason. No input event is sent. |
| **Run this step** | Runs that one step against the page as it is now, then reports what happened. |
| **Discard plan** | Takes the plan off the card and keeps your goal text for another try. |

A step can do four things: **Open** a web address, **Click** a control, **Type into** a control,
**Press** one of the 14 keys. Steps name controls rather than remembering coordinates, so a form
that moves after a navigation is still clicked in the right place — and a control that has
disappeared stops that step with `That control is no longer on the page. Ask for a new plan.`
instead of clicking where it used to be.

A type step carries no text until you run it: the panel asks in a masked field, sends it once,
and reports only how many characters arrived. Keep passwords out of the goal — the goal goes to
your provider.

What the provider is shown is the page's own address and the labels of the controls on it. It is
never shown your browser's DevTools address or token, and its answer is checked against the page
before it reaches the card.

## Privacy and consent

- Text you type and codes you send go straight into the browser. They are not stored, not
  repeated in any API response, and only counted in the activity log ("Typed 13 characters
  into a page").
- The generated code is typed server-side and never returned to the dashboard, so the only
  place it appears is the browser you are driving.
- Frames are streamed to you and not written to disk or to a database. A recording exists
  only while the view is open and only in the container's memory; a download is the one
  place a frame becomes a file, and it is on your machine.
- The live stream is authenticated like every other endpoint and is scoped to the operator
  who saved the address. Frames never cross between operators.
- Named browsers are scoped the same way, and the list handed back to the panel carries a
  label, a host and whether a token is stored — never the DevTools address or the token.
  Their cookies live in the browser you connected to, so this product copies nothing between
  them.
- A drafted plan holds nothing worth stealing: an action, a validated address, a key, or a
  control's description. The text you type into a step never enters the plan, the provider's
  prompt, the API response or the log — the log says `Ran step 2 of a browser plan (type)` and
  the plan itself says "Typed 13 characters".
- The AI agent drafts and the operator approves. No step, and no sequence of steps, runs by
  itself, on a timer, or when the page changes.
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
| `Open the live view before recording.` | Recording writes into the session a live view opened. Press **Start live view** first. |
| `This browser view already has the maximum number of watchers.` | Four people watch that page. Ask one to close their tab, or stop and restart the view. |
| `The hosted browser closed this view.` | The browser ended the DevTools session — it restarted, the page was closed, or the vendor cut the connection. Start the view again. |
| `Keep at most 12 named browsers - forget one first.` | The browser list is full. **Forget** one before adding another. |
| `No named browser with that id.` | The row you acted on is from a panel that has not reloaded — another tab just changed the list. Reload the tab. |
| `Configure an AI provider and API key in Settings → AI providers.` | The plan card has nothing to ask. Set a provider first; the plan uses your own key, never a built-in one. |
| `The provider did not return a usable plan.` / `The provider proposed no steps.` | The answer was prose, empty or not a list of steps. Try a narrower goal. |
| `That plan asks for something the browser cannot do from here.` | The provider named an action this panel will not take, or an address that is not a web page. |
| `That plan points at a field the page does not have.` | The provider invented a control. Ask for a new plan so it reads the page again. |
| `The page would not list its controls.` | The page refused to describe itself — a DevTools-restricted vendor, or a page that is not really there. |
| `That plan is no longer available.` | It ran out (15 minutes), was discarded, or belongs to a browser you have since switched away from. |
| `Run the steps in order.` | A later step cannot be pressed before an earlier one has run. |
| `Type the text this step should send first.` | A type step needs its text in the masked field beside its button. |
| `That control is no longer on the page. Ask for a new plan.` | The page changed under the plan. Propose again; the new plan reads the page as it is. |

Design decisions and the endpoint contract live in
[docs/superpowers/specs/2026-11-01-cloud-browser-design.md](superpowers/specs/2026-11-01-cloud-browser-design.md).
