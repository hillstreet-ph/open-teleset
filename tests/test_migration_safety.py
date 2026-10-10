"""Legacy migrations must never replace canonical shared Auth contracts."""
from unittest.mock import AsyncMock
import ssl

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


def test_configured_dedicated_endpoint_is_not_rewritten(monkeypatch):
    monkeypatch.delenv("DATABASE_POOLER_URL", raising=False)
    dsn = "postgresql://postgres@db.hoseohvgoiarxluxqwqv.supabase.co:6543/postgres?sslmode=verify-full"
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("SUPABASE_REGION", "untrusted-region")
    assert migrations._candidate_dsns() == [dsn]


def test_candidate_dsns_preserves_explicit_preference_and_deduplicates(monkeypatch):
    pooler = "postgresql://postgres@dedicated.example:6543/postgres?sslmode=verify-full"
    direct = "postgresql://postgres@direct.example:5432/postgres?sslmode=verify-full"
    monkeypatch.setenv("DATABASE_POOLER_URL", pooler)
    monkeypatch.setenv("DATABASE_URL", direct)
    assert migrations._candidate_dsns() == [pooler, direct]
    monkeypatch.setenv("DATABASE_URL", pooler)
    assert migrations._candidate_dsns() == [pooler]


@pytest.mark.parametrize("placeholder", ["YOUR_PASSWORD", "YOUR-PASSWORD"])
def test_placeholder_connection_is_not_attempted(monkeypatch, placeholder):
    monkeypatch.delenv("DATABASE_POOLER_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://postgres:{placeholder}@localhost/postgres")
    assert migrations._candidate_dsns() == []


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


@pytest.mark.asyncio
async def test_database_ca_keeps_hostname_and_certificate_verification(monkeypatch):
    certificate = migrations.ROOT / "deploy/certs/supabase-prod-ca-2021.crt"
    monkeypatch.setenv("PGSSLROOTCERT", str(certificate))
    connect = AsyncMock(return_value=object())
    monkeypatch.setattr(migrations.asyncpg, "connect", connect)
    await migrations._connect(["postgresql://localhost/postgres"])
    context = connect.call_args.kwargs["ssl"]
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] == 1


@pytest.mark.asyncio
async def test_bad_database_ca_fails_before_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("PGSSLROOTCERT", str(tmp_path / "missing.crt"))
    connect = AsyncMock()
    monkeypatch.setattr(migrations.asyncpg, "connect", connect)
    with pytest.raises(FileNotFoundError):
        await migrations._connect(["postgresql://localhost/postgres"])
    connect.assert_not_awaited()
