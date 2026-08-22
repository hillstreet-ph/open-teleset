"""Background health monitor."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

from open_teleset.account_store import AccountStore
from open_teleset.db import close_pool, init_pool, record_health

logger = logging.getLogger("health_worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


async def check_one(account: dict) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    account_id = str(account["id"])
    store = AccountStore()
    session = await store.get_session_string(account_id)
    if not session:
        await record_health(account_id, False, error_message="no session")
        return

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    client = TelegramClient(StringSession(session), api_id, api_hash)

    t0 = time.perf_counter()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await record_health(account_id, False, error_message="not authorized")
            return
        await client.get_me()
        latency = int((time.perf_counter() - t0) * 1000)
        await record_health(account_id, True, latency_ms=latency)
        logger.info("healthy %s latency=%dms", account_id, latency)
    except Exception as e:
        await record_health(account_id, False, error_message=str(e)[:500])
        logger.warning("unhealthy %s: %s", account_id, e)
    finally:
        await client.disconnect()


async def loop() -> None:
    interval = int(os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "300"))
    store = AccountStore()
    await init_pool()
    try:
        while True:
            accounts = await store.list(status="active")
            for acc in accounts:
                try:
                    await check_one(acc)
                except Exception as e:
                    logger.exception("check failed: %s", e)
            await asyncio.sleep(interval)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(loop())
