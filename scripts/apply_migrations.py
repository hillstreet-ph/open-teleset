#!/usr/bin/env python3
"""Apply SQL migrations from migrations/ to DATABASE_URL."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn or "YOUR_PASSWORD" in dsn:
        print("DATABASE_URL not configured — abort", file=sys.stderr)
        return 1

    migrations_dir = ROOT / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        print("No migration files found")
        return 0

    conn = await asyncpg.connect(dsn)
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
