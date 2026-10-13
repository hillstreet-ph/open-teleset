"""Legacy migrations must never replace canonical shared Auth contracts."""
from unittest.mock import AsyncMock

import pytest

from scripts import apply_migrations as migrations


@pytest.fixture
def target(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    conn = AsyncMock()
    conn.fetchval.return_value = True
    monkeypatch.setattr(migrations, "_candidate_dsns", lambda: ["postgresql://local/standalone"])
    monkeypatch.setattr(migrations, "_connect", AsyncMock(return_value=conn))
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_present", [False, True])
async def test_shared_target_fails_before_any_migration_or_ledger_write(target, ledger_present, capsys):
    # The actual repository migration directory includes the incompatible
    # profiles.email insert and shared signup-trigger replacement in 002.
    target.fetch.return_value = ([{"filename": "002_auth.sql"}] if ledger_present else [])
    assert await migrations.main() == 1
    target.fetchval.assert_awaited_once_with(migrations.SHARED_TARGET_QUERY)
    target.execute.assert_not_awaited()
    target.fetch.assert_not_awaited()
    target.transaction.assert_not_called()
    target.close.assert_awaited_once()
    assert "preserve shared profiles, roles and signup triggers" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("dsn", [
    "postgresql://postgres@db.hoseohvgoiarxluxqwqv.supabase.co/postgres",
    "postgresql://postgres.hoseohvgoiarxluxqwqv@aws-0-ap-southeast-1.pooler.supabase.com/postgres",
    "postgresql://postgres@db.huadtiuuoiriqrjpjxhr.supabase.co/postgres",
])
async def test_recognized_shared_target_is_a_noop_and_never_runs_legacy_sql(target, monkeypatch, dsn):
    # A recognized shared project (its schema is owned by Supabase-managed
    # migrations) is a successful no-op: the legacy set never runs there, and the
    # release pipeline is not blocked. The guard still refuses to execute any SQL
    # and does not even open a connection.
    monkeypatch.setattr(migrations, "_candidate_dsns", lambda: [dsn])
    target.fetchval.return_value = False
    assert await migrations.main() == 0
    target.execute.assert_not_awaited()
    target.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_supabase_url_is_a_noop_for_opaque_database_proxy(target, monkeypatch):
    # An opaque DSN paired with a canonical SUPABASE_URL is still recognized as a
    # shared target and treated as a no-op, without running legacy SQL.
    monkeypatch.setenv("SUPABASE_URL", "https://hoseohvgoiarxluxqwqv.supabase.co")
    target.fetchval.return_value = False
    assert await migrations.main() == 0
    target.execute.assert_not_awaited()
    target.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverifiable_schema_fails_without_writes(target):
    target.fetchval.side_effect = RuntimeError("schema discovery unavailable")
    with pytest.raises(RuntimeError, match="schema discovery unavailable"):
        await migrations.main()
    target.execute.assert_not_awaited()
    target.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_legacy_target_can_pass_guard(target):
    target.fetchval.return_value = False
    await migrations._validate_legacy_target(target, ["postgresql://localhost/standalone"])
    target.fetchval.assert_awaited_once_with(migrations.SHARED_TARGET_QUERY)


@pytest.mark.asyncio
async def test_unexpected_schema_result_fails_closed(target):
    target.fetchval.return_value = None
    with pytest.raises(migrations.MigrationSafetyError):
        await migrations._validate_legacy_target(target, ["postgresql://localhost/standalone"])
    target.execute.assert_not_awaited()
