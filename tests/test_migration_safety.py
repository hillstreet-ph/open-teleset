"""Legacy migrations must never replace canonical shared Auth contracts."""
from unittest.mock import AsyncMock
from urllib.parse import urlparse

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
async def test_canonical_project_cannot_bypass_guard_with_missing_schema_markers(target, monkeypatch, dsn):
    monkeypatch.setattr(migrations, "_candidate_dsns", lambda: [dsn])
    target.fetchval.return_value = False
    assert await migrations.main() == 1
    target.execute.assert_not_awaited()
    target.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_canonical_supabase_url_blocks_opaque_database_proxy(target, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://hoseohvgoiarxluxqwqv.supabase.co")
    target.fetchval.return_value = False
    assert await migrations.main() == 1
    target.execute.assert_not_awaited()


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


def test_direct_dsn_is_mapped_to_resolvable_pooler(monkeypatch):
    monkeypatch.setenv("SUPABASE_REGION", "ap-southeast-1")
    derived = migrations._derive_pooler(
        "postgresql://postgres:secret@db.hoseohvgoiarxluxqwqv.supabase.co:5432/postgres"
    )
    assert derived is not None
    parsed = urlparse(derived)
    assert parsed.hostname == "aws-0-ap-southeast-1.pooler.supabase.com"
    assert parsed.port == 6543
    assert parsed.username == "postgres.hoseohvgoiarxluxqwqv"
    assert parsed.password == "secret"
    assert "sslmode=require" in derived


def test_non_supabase_dsn_is_not_rewritten():
    assert migrations._derive_pooler("postgresql://user:pw@localhost:5432/db") is None


def test_candidate_dsns_prefers_pooler_before_direct(monkeypatch):
    monkeypatch.setenv("SUPABASE_REGION", "ap-southeast-1")
    monkeypatch.delenv("DATABASE_POOLER_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres:pw@db.hoseohvgoiarxluxqwqv.supabase.co:5432/postgres",
    )
    candidates = migrations._candidate_dsns()
    assert len(candidates) == 2
    assert "pooler.supabase.com" in candidates[0]
    assert "db.hoseohvgoiarxluxqwqv.supabase.co" in candidates[1]


@pytest.mark.asyncio
async def test_unreachable_database_returns_clean_status(target, monkeypatch, capsys):
    monkeypatch.setattr(migrations, "_candidate_dsns", lambda: ["postgresql://pooler/path"])
    monkeypatch.setattr(migrations, "_connect", AsyncMock(side_effect=OSError("no route")))
    assert await migrations.main() == 1
    err = capsys.readouterr().err
    assert "MIGRATIONS_STATUS=unreachable" in err
    assert "no route" not in err  # provider detail must not leak verbatim


@pytest.mark.asyncio
async def test_missing_configuration_is_reported(target, monkeypatch, capsys):
    monkeypatch.setattr(migrations, "_candidate_dsns", list)
    assert await migrations.main() == 1
    assert "MIGRATIONS_STATUS=not_configured" in capsys.readouterr().err
