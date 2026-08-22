"""Supabase / Postgres access layer."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import asyncpg
from supabase import Client, create_client

_pool: Optional[asyncpg.Pool] = None
_supabase: Optional[Client] = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        _supabase = create_client(url, key)
    return _supabase


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    dsn = os.getenv("DATABASE_POOLER_URL") or os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=10,
        command_timeout=30,
        statement_cache_size=0,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn


async def list_accounts(status: Optional[str] = None) -> list[dict[str, Any]]:
    async with acquire() as conn:
        if status:
            rows = await conn.fetch(
                "select * from accounts where status = $1 order by created_at desc",
                status,
            )
        else:
            rows = await conn.fetch("select * from accounts order by created_at desc")
    return [dict(r) for r in rows]


async def get_account(account_id: str) -> Optional[dict[str, Any]]:
    async with acquire() as conn:
        row = await conn.fetchrow("select * from accounts where id = $1::uuid", account_id)
    return dict(row) if row else None


async def upsert_account(
    *,
    account_id: Optional[str] = None,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    username: Optional[str] = None,
    session_encrypted: Optional[str] = None,
    status: str = "active",
    proxy_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict[str, Any]:
    async with acquire() as conn:
        if account_id:
            row = await conn.fetchrow(
                """
                update accounts set
                  name = coalesce($2, name),
                  phone = coalesce($3, phone),
                  username = coalesce($4, username),
                  session_encrypted = coalesce($5, session_encrypted),
                  status = $6,
                  proxy_id = coalesce($7::uuid, proxy_id),
                  metadata = coalesce($8::jsonb, metadata),
                  updated_at = now()
                where id = $1::uuid
                returning *
                """,
                account_id, name, phone, username, session_encrypted, status, proxy_id, metadata,
            )
        else:
            row = await conn.fetchrow(
                """
                insert into accounts (name, phone, username, session_encrypted, status, proxy_id, metadata)
                values ($1, $2, $3, $4, $5, $6::uuid, coalesce($7::jsonb, '{}'::jsonb))
                returning *
                """,
                name, phone, username, session_encrypted, status, proxy_id, metadata,
            )
    return dict(row)


async def delete_account(account_id: str) -> bool:
    async with acquire() as conn:
        result = await conn.execute("delete from accounts where id = $1::uuid", account_id)
    return result.endswith("1")


async def add_log(
    action: str,
    *,
    level: str = "info",
    account_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            insert into operation_logs (level, action, account_id, details)
            values ($1, $2, $3::uuid, coalesce($4::jsonb, '{}'::jsonb))
            """,
            level, action, account_id, details,
        )


async def record_health(
    account_id: str,
    is_healthy: bool,
    latency_ms: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            insert into health_checks (account_id, is_healthy, latency_ms, error_message)
            values ($1::uuid, $2, $3, $4)
            """,
            account_id, is_healthy, latency_ms, error_message,
        )
        if is_healthy:
            await conn.execute(
                "update accounts set last_active_at = now(), status = 'active' where id = $1::uuid",
                account_id,
            )
