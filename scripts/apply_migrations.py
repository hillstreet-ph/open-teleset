#!/usr/bin/env python3
"""Apply SQL migrations from migrations/ to DATABASE_URL / pooler."""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _ensure_sslmode(dsn: str) -> str:
    if "sslmode=" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}sslmode=require"


def _candidate_dsns() -> list[str]:
    """Prefer transaction/session pooler (IPv4) over direct db host (often IPv6-only)."""
    out: list[str] = []
    for key in ("DATABASE_POOLER_URL", "DATABASE_URL"):
        v = (os.getenv(key) or "").strip()
        if v and "YOUR_PASSWORD" not in v:
            out.append(_ensure_sslmode(v))
    # Derive Supabase pooler host from project ref if only direct URL given
    direct = (os.getenv("DATABASE_URL") or "").strip()
    if direct and "supabase.co" in direct and "pooler.supabase.com" not in direct:
        try:
            u = urlparse(direct)
            # postgres:pass@db.REF.supabase.co -> postgres.REF:pass@aws-0-REGION.pooler.supabase.com
            host = u.hostname or ""
            if host.startswith("db.") and host.endswith(".supabase.co"):
                ref = host[len("db.") : -len(".supabase.co")]
                region = os.getenv("SUPABASE_REGION", "ap-southeast-1")
                user = u.username or "postgres"
                # pooler user form: postgres.ref
                pool_user = f"postgres.{ref}" if "." not in user else user
                password = u.password or ""
                pool_host = f"aws-0-{region}.pooler.supabase.com"
                auth = f"{pool_user}:{password}@" if password else f"{pool_user}@"
                derived = f"postgresql://{auth}{pool_host}:6543{u.path or '/postgres'}?sslmode=require"
                if derived not in out:
                    out.insert(0, derived)
        except Exception as e:
            print(f"pooler derive skipped: {e}")
    return out


async def _connect(dsns: list[str]):
    last = None
    ctx = ssl.create_default_context()
    for dsn in dsns:
        try:
            print(f"Connecting (host hidden)...")
            conn = await asyncpg.connect(dsn, ssl=ctx, statement_cache_size=0, timeout=60)
            return conn
        except Exception as e:
            last = e
            print(f"  connect failed: {type(e).__name__}: {e}")
    raise last or RuntimeError("No DATABASE_URL configured")


async def main() -> int:
    dsns = _candidate_dsns()
    if not dsns:
        print("DATABASE_URL not configured — abort", file=sys.stderr)
        return 1

    migrations_dir = ROOT / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        print("No migration files found")
        return 0

    conn = await _connect(dsns)
    try:
        await conn.execute(
            """
            create table if not exists _schema_migrations (
              filename text primary key,
              applied_at timestamptz not null default now()
            )
            """
        )
        applied = {
            r["filename"]
            for r in await conn.fetch("select filename from _schema_migrations")
        }
        for path in files:
            name = path.name
            if name in applied:
                print(f"  skip {name}")
                continue
            sql = path.read_text(encoding="utf-8")
            print(f"  apply {name} ...")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "insert into _schema_migrations (filename) values ($1)", name
                )
            print(f"  ok   {name}")
    finally:
        await conn.close()
    print("Migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
