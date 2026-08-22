#!/usr/bin/env python3
"""Migrate accounts/config.json sessions into Supabase (encrypted)."""
from __future__ import annotations
import asyncio, json, os, sys
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))
from open_teleset.account_store import AccountStore
from open_teleset.db import close_pool, init_pool

async def main() -> int:
    config_path = ROOT / "accounts" / "config.json"
    if not config_path.exists():
        config_path = Path("accounts/config.json")
    if not config_path.exists():
        print("No accounts/config.json — nothing to migrate")
        return 0
    data = json.loads(config_path.read_text(encoding="utf-8"))
    await init_pool()
    store = AccountStore()
    n = 0
    try:
        for key, acc in data.items():
            session = acc.get("session_string") or acc.get("session") or ""
            if not session:
                continue
            await store.save_session(
                session_string=session,
                name=acc.get("name") or key,
                phone=acc.get("phone"),
                username=acc.get("username"),
                status=acc.get("status") or "active",
            )
            n += 1
            print("migrated", key)
    finally:
        await close_pool()
    print(f"Done — {n} accounts")
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
