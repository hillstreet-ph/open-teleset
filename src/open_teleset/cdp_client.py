"""Chrome DevTools Protocol client for the Cloud Browser panel.

Only the slice of CDP the dashboard needs is implemented: list the pages a
hosted browser has open, look at one, drive a login form and close a tab again.
Any endpoint that speaks CDP works - a hosted-browser vendor or the operator's
own Chromium behind a tunnel - so no vendor is baked into the product.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Control keys the dashboard may send. Text goes through Input.insertText
# instead, so this list stays the non-printing keys a login flow needs.
_KEY_CODES: dict[str, dict[str, Any]] = {
    "Enter": {"key": "Enter", "code": "Enter", "vk": 13},
    "Tab": {"key": "Tab", "code": "Tab", "vk": 9},
    "Escape": {"key": "Escape", "code": "Escape", "vk": 27},
    "Backspace": {"key": "Backspace", "code": "Backspace", "vk": 8},
    "Delete": {"key": "Delete", "code": "Delete", "vk": 46},
    "Space": {"key": " ", "code": "Space", "vk": 32},
    "Home": {"key": "Home", "code": "Home", "vk": 36},
    "End": {"key": "End", "code": "End", "vk": 35},
    "PageUp": {"key": "PageUp", "code": "PageUp", "vk": 33},
    "PageDown": {"key": "PageDown", "code": "PageDown", "vk": 34},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "vk": 38},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "vk": 39},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
}

KEY_REJECTED_MESSAGE = "That key cannot be sent from the dashboard."
_BUTTON_MASK = {"left": 1, "middle": 4, "right": 8}


def pressable_keys() -> list[str]:
    """The allowlist the cockpit renders, so the UI can never offer a dead key."""
    return list(_KEY_CODES)


class CdpError(RuntimeError):
    """The browser endpoint refused, timed out or could not be reached."""


class CdpClient:
    def __init__(self, websocket, session=None) -> None:
        self._ws = websocket
        self._session = session
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: dict[str, list] = {}
        self._screencasts: dict[str, Any] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None

    @classmethod
    async def connect(cls, url: str, headers: dict | None = None, timeout: float = 10.0) -> CdpClient:
        import aiohttp

        session = None
        try:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=timeout, sock_connect=timeout)
            )
            websocket = await asyncio.wait_for(
                session.ws_connect(url, headers=headers, max_msg_size=16 * 1024 * 1024), timeout
            )
        except TimeoutError:
            if session:
                await session.close()
            raise CdpError("The browser endpoint did not accept a DevTools connection in time.") from None
        except Exception:
            # Any transport failure is reported the same way: the address and token stay out of the message.
            if session:
                await session.close()
            raise CdpError("Could not reach the browser endpoint. Check the address and that it is running.") from None
        instance = cls(websocket, session)
        instance._reader = asyncio.create_task(instance._read_loop())
        return instance

    @property
    def is_closed(self) -> bool:
        """True once the socket has gone; the caller then makes a fresh connection."""
        return self._ws.closed

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._listeners.clear()
        self._screencasts.clear()

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                # aiohttp hands back WSMessage; anything that is not text is not a CDP frame.
                raw = getattr(message, "data", message)
                if not isinstance(raw, str):
                    continue
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(frame)
        except Exception:
            pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CdpError("The DevTools connection closed before answering."))
        self._pending.clear()

    def _dispatch(self, frame: dict) -> None:
        request_id = frame.get("id")
        if not isinstance(request_id, int):
            method = frame.get("method")
            if isinstance(method, str) and self._listeners.get(method):
                # Handlers await, so events go through a pump rather than the
                # read loop: a screencast ack issued from the read loop would
                # wait for a reply only the read loop can deliver.
                self._events.put_nowait((method, frame.get("sessionId") or "", frame.get("params") or {}))
            return
        future = self._pending.get(request_id)
        if not future or future.done():
            return
        error = frame.get("error")
        if error:
            future.set_exception(CdpError("The browser endpoint refused the DevTools command."))
        else:
            future.set_result(frame.get("result") or {})

    def subscribe(self, method: str, handler) -> None:
        self._listeners.setdefault(method, []).append(handler)
        if self._pump is None:
            self._pump = asyncio.create_task(self._event_pump())

    def unsubscribe(self, method: str, handler) -> None:
        listeners = self._listeners.get(method)
        if listeners and handler in listeners:
            listeners.remove(handler)

    async def _event_pump(self) -> None:
        while True:
            method, session_id, params = await self._events.get()
            for handler in list(self._listeners.get(method, ())):
                try:
                    await handler(params, session_id)
                except Exception:
                    # A handler that fails (an ack on a page that just closed)
                    # must not stop the frames for every other subscriber.
                    continue

    async def call(self, method: str, params: dict | None = None, session_id: str = "", timeout: float = 15.0) -> dict:
        if self._ws.closed:
            raise CdpError("The DevTools connection is closed. Reconnect from the Cloud Browser panel.")
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._ws.send_str(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise CdpError(f"The browser endpoint did not answer: {method}") from None
        except ConnectionResetError:
            raise CdpError("The DevTools connection dropped mid-command.") from None
        finally:
            self._pending.pop(request_id, None)

    async def pages(self) -> list:
        targets = await self.call("Target.getTargets")
        return [
            {
                "target_id": item.get("targetId", ""),
                "title": item.get("title") or "",
                "url": item.get("url") or "",
            }
            for item in targets.get("targetInfos", [])
            if item.get("type") == "page"
        ]

    async def _page_session(self, target_id: str) -> str:
        if not target_id:
            raise CdpError("Select an open page first.")
        attached = await self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = attached.get("sessionId") or ""
        if not session_id:
            raise CdpError("The browser endpoint would not attach to that page.")
        await self.call("Page.enable", {}, session_id)
        return session_id

    async def open_page(self, url: str) -> dict:
        created = await self.call("Target.createTarget", {"url": url})
        return {"target_id": created.get("targetId", "")}

    async def close_page(self, target_id: str) -> None:
        await self.call("Target.closeTarget", {"targetId": target_id})

    async def navigate(self, target_id: str, url: str) -> None:
        await self.call("Page.navigate", {"url": url}, await self._page_session(target_id))

    async def screenshot(self, target_id: str) -> str:
        captured = await self.call(
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": 60},
            await self._page_session(target_id),
        )
        return captured.get("data") or ""

    async def type_text(self, target_id: str, text: str) -> None:
        await self.call("Input.insertText", {"text": text}, await self._page_session(target_id))

    async def press_key(self, target_id: str, key: str) -> None:
        spec = _KEY_CODES.get(key)
        if not spec:
            raise CdpError(KEY_REJECTED_MESSAGE)
        session_id = await self._page_session(target_id)
        base = {
            "key": spec["key"],
            "code": spec["code"],
            "windowsVirtualKeyCode": spec["vk"],
            "nativeVirtualKeyCode": spec["vk"],
        }
        await self.call("Input.dispatchKeyEvent", {"type": "keyDown", **base}, session_id)
        await self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base}, session_id)

    async def start_screencast(self, target_id: str, on_frame, quality: int = 60,
                               max_width: int = 1280, max_height: int = 800) -> str:
        """Stream JPEG frames; returns the page session the frames belong to."""
        session_id = await self._page_session(target_id)

        async def forward(params: dict, event_session: str) -> None:
            if event_session and event_session != session_id:
                return
            # Chrome holds the stream back until the newest frame is acknowledged,
            # so ack before handing the frame on rather than after.
            await self.call("Page.screencastFrameAck", {"frameMetadata": params.get("metadata") or {}}, session_id)
            data = params.get("data") or ""
            if data:
                on_frame(data, params.get("metadata") or {})

        self.subscribe("Page.screencastFrame", forward)
        self._screencasts[session_id] = forward
        await self.call(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": quality,
                "maxWidth": max_width,
                "maxHeight": max_height,
                "everyNthFrame": 1,
            },
            session_id,
        )
        return session_id

    async def stop_screencast(self, session_id: str) -> None:
        handler = self._screencasts.pop(session_id, None)
        if handler:
            self.unsubscribe("Page.screencastFrame", handler)
        if self._ws.closed:
            return
        await self.call("Page.stopScreencast", {}, session_id)

    async def viewport_size(self, target_id: str) -> tuple[float, float]:
        """CSS pixels of the visible viewport - the coordinate space a frame is drawn in."""
        metrics = await self.call("Page.getLayoutMetrics", {}, await self._page_session(target_id))
        for key in ("cssVisualViewport", "visualViewport", "contentSize"):
            box = metrics.get(key) or {}
            width, height = _positive(box.get("width")), _positive(box.get("height"))
            if width and height:
                return width, height
        raise CdpError("The browser endpoint did not report the page size.")

    async def move_mouse(self, target_id: str, x: float, y: float) -> None:
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0, "clickCount": 0},
            await self._page_session(target_id),
        )

    async def click_mouse(self, target_id: str, x: float, y: float, button: str = "left",
                          click_count: int = 1) -> None:
        mask = _BUTTON_MASK[button]
        session_id = await self._page_session(target_id)
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0, "clickCount": 0},
            session_id,
        )
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": button, "buttons": mask, "clickCount": click_count},
            session_id,
        )
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": button, "buttons": 0, "clickCount": click_count},
            session_id,
        )

    async def scroll_page(self, target_id: str, x: float, y: float, delta_x: float = 0.0,
                          delta_y: float = 0.0) -> None:
        await self.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "button": "none",
                "buttons": 0,
                "clickCount": 0,
                "deltaX": delta_x,
                "deltaY": delta_y,
            },
            await self._page_session(target_id),
        )

    async def version(self) -> dict:
        info = await self.call("Browser.getVersion")
        return {
            "product": str(info.get("product") or "")[:60],
            "revision": str(info.get("revision") or "")[:30],
        }


def _positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0

