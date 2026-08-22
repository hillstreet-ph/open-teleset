"""Account store backed by Supabase Postgres + encrypted sessions."""

from __future__ import annotations

from typing import Any, Optional

from open_teleset.crypto import decrypt_session, encrypt_session
from open_teleset.db import add_log, delete_account, get_account, list_accounts, upsert_account


class AccountStore:
    async def list(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        rows = await list_accounts(status=status)
        out = []
        for r in rows:
            item = dict(r)
            item.pop("session_encrypted", None)
            item["has_session"] = bool(r.get("session_encrypted"))
            out.append(item)
        return out

    async def get(self, account_id: str) -> Optional[dict[str, Any]]:
        row = await get_account(account_id)
        if not row:
            return None
        item = dict(row)
        item.pop("session_encrypted", None)
        item["has_session"] = bool(row.get("session_encrypted"))
        return item

    async def get_session_string(self, account_id: str) -> Optional[str]:
        row = await get_account(account_id)
        if not row or not row.get("session_encrypted"):
            return None
        return decrypt_session(row["session_encrypted"])

    async def save_session(
        self,
        *,
        account_id: Optional[str] = None,
        session_string: str,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        username: Optional[str] = None,
        proxy_id: Optional[str] = None,
        status: str = "active",
    ) -> dict[str, Any]:
        encrypted = encrypt_session(session_string)
        row = await upsert_account(
            account_id=account_id,
            name=name,
            phone=phone,
            username=username,
            session_encrypted=encrypted,
            status=status,
            proxy_id=proxy_id,
        )
        await add_log(
            "account.save_session",
            account_id=str(row["id"]),
            details={"phone": phone, "username": username},
        )
        item = dict(row)
        item.pop("session_encrypted", None)
        return item

    async def remove(self, account_id: str) -> bool:
        ok = await delete_account(account_id)
        if ok:
            await add_log("account.delete", account_id=account_id)
        return ok
