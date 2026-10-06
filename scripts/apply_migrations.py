#!/usr/bin/env python3
"""Apply SQL migrations from migrations/ to DATABASE_URL / pooler."""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# These databases host several applications. The legacy migration set below
# owns public objects and an auth.users trigger, so it must never run there.
SHARED_PROJECT_REFS = {"hoseohvgoiarxluxqwqv", "huadtiuuoiriqrjpjxhr"}
SHARED_TARGET_QUERY = """
select
  exists (
    select 1 from pg_namespace
    where nspname in ('operations_shared', 'platform_shared', 'open_teleset')
  ) or (
    to_regclass('public.profiles') is not null
    and not exists (
      select 1 from information_schema.columns
      where table_schema = 'public' and table_name = 'profiles'
        and column_name = 'email'
    )
  )
"""


class MigrationSafetyError(RuntimeError):
    """The legacy migration set cannot safely own this database's objects."""


def _shared_project_configured(dsns: list[str]) -> bool:
    for address in [*dsns, os.getenv("SUPABASE_URL", "")]:
        parsed = urlparse(address)
        host = parsed.hostname or ""
        user = parsed.username or ""
        if any(host in {f"{ref}.supabase.co", f"db.{ref}.supabase.co"}
               or user.endswith(f".{ref}") for ref in SHARED_PROJECT_REFS):
            return True
    return False


async def _validate_legacy_target(conn, dsns: list[str]) -> None:
    # Read-only validation precedes even creation of the migration ledger.
    # A prior legacy ledger does not authorize replacement of shared auth.
    if _shared_project_configured(dsns) or await conn.fetchval(SHARED_TARGET_QUERY) is not False:
        raise MigrationSafetyError(
            "Legacy public migrations are blocked on shared or unverified targets. "
            "Deploy reviewed open_teleset-scoped migrations and a project-scoped "
            "ledger first; preserve shared profiles, roles and signup triggers. "
            "No migration or ledger write was performed."
        )


def _ensure_sslmode(dsn: str) -> str:
    if "sslmode=" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}sslmode=require"


def _derive_pooler(dsn: str) -> str | None:
    """Map a Supabase direct DSN to its IPv4 pooler equivalent.

    Direct `db.<ref>.supabase.co` hosts are frequently IPv6-only or absent from
    CI DNS. The Supavisor pooler host (`aws-0-<region>.pooler.supabase.com`)
    resolves everywhere and is the supported connection path for CI/runtime.
    """
    try:
        parsed = urlparse(dsn)
    except ValueError:
        return None
    host = parsed.hostname or ""
    if not (host.startswith("db.") and host.endswith(".supabase.co")):
        return None
    ref = host[len("db.") : -len(".supabase.co")]
    region = os.getenv("SUPABASE_REGION", "ap-southeast-1")
    user = parsed.username or "postgres"
    pool_user = user if "." in user else f"postgres.{ref}"
    password = parsed.password or ""
    auth = f"{pool_user}:{password}@" if password else f"{pool_user}@"
    pool_host = f"aws-0-{region}.pooler.supabase.com"
    return f"postgresql://{auth}{pool_host}:6543{parsed.path or '/postgres'}?sslmode=require"


def _candidate_dsns() -> list[str]:
    """Pooler first, then direct — every resolvable Supabase variant is offered."""
    poolers: list[str] = []
    directs: list[str] = []
    for key in ("DATABASE_POOLER_URL", "DATABASE_URL"):
        value = (os.getenv(key) or "").strip()
        if not value or "YOUR_PASSWORD" in value:
            continue
        normalized = _ensure_sslmode(value)
        if "pooler.supabase.com" in normalized:
            poolers.append(normalized)
            continue
        directs.append(normalized)
        derived = _derive_pooler(normalized)
        if derived:
            poolers.append(derived)

    ordered: list[str] = []
    for candidate in [*poolers, *directs]:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


async def _connect(dsns: list[str]):
    last = None
    ctx = ssl.create_default_context()
    for dsn in dsns:
        try:
            print("Connecting (host hidden)...")
            conn = await asyncpg.connect(dsn, ssl=ctx, statement_cache_size=0, timeout=60)
            return conn
        except Exception as e:
            last = e
            # Only the exception class is safe to log; messages can embed hosts.
            print(f"  connect failed: {type(e).__name__}")
    raise last or RuntimeError("No DATABASE_URL configured")


async def main() -> int:
    dsns = _candidate_dsns()
    if not dsns:
        print("MIGRATIONS_STATUS=not_configured", file=sys.stderr)
        print(
            "DATABASE_URL / DATABASE_POOLER_URL not configured — no legacy "
            "migrations to apply. Runtime readiness is verified separately by /readyz.",
            file=sys.stderr,
        )
        return 1

    migrations_dir = ROOT / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        print("MIGRATIONS_STATUS=no_files")
        print("No migration files found")
        return 0

    try:
        conn = await _connect(dsns)
    except Exception as exc:
        print("MIGRATIONS_STATUS=unreachable", file=sys.stderr)
        print(
            f"Could not reach the configured database ({type(exc).__name__}): "
            "verify the connection reference, project ref and network egress. "
            "No migration or ledger write was performed.",
            file=sys.stderr,
        )
        return 1
    try:
        try:
            await _validate_legacy_target(conn, dsns)
        except MigrationSafetyError as exc:
            print("MIGRATIONS_STATUS=not_applicable", file=sys.stderr)
            print(str(exc), file=sys.stderr)
            return 1
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
    print("MIGRATIONS_STATUS=applied")
    print("Migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
