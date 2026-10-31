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

_KEY_CODES = {
    "Enter": (13, "Enter"),
    "Tab": (9, "Tab"),
    "Escape": (27, "Escape"),
    "Backspace": (8, "Backspace"),
}


class CdpError(RuntimeError):
    """The browser endpoint refused, timed out or could not be reached."""


class CdpClient:
    def __init__(self, websocket, session=None) -> None:
        self._ws = websocket
        self._session = session
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
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

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

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
            return
        future = self._pending.get(request_id)
        if not future or future.done():
            return
        error = frame.get("error")
        if error:
            future.set_exception(CdpError("The browser endpoint refused the DevTools command."))
        else:
            future.set_result(frame.get("result") or {})

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
        if key not in _KEY_CODES:
            raise CdpError("Only Enter, Tab, Escape and Backspace can be sent.")
        code, name = _KEY_CODES[key]
        session_id = await self._page_session(target_id)
        base = {"key": name, "code": name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        await self.call("Input.dispatchKeyEvent", {"type": "keyDown", **base}, session_id)
        await self.call("Input.dispatchKeyEvent", {"type": "keyUp", **base}, session_id)

    async def version(self) -> dict:
        info = await self.call("Browser.getVersion")
        return {
            "product": str(info.get("product") or "")[:60],
            "revision": str(info.get("revision") or "")[:30],
        }
