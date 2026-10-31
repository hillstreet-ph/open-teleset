"""Scraper failures must be actionable and must not leak Telegram internals."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import telegram_ops as ops


def fake_client(entity=None, participants=None, error=None):
    get_entity = AsyncMock(return_value=entity, side_effect=error)
    get_participants = AsyncMock(return_value=participants or [])
    return SimpleNamespace(get_entity=get_entity, get_participants=get_participants)


@pytest.mark.asyncio
async def test_tme_url_is_resolved_to_a_username(monkeypatch):
    client = fake_client(entity=SimpleNamespace())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="https://t.me/example_channel")
    )
    assert result["success"] is True
    client.get_entity.assert_awaited_once_with("@example_channel")


@pytest.mark.asyncio
async def test_numeric_channel_id_is_sent_as_an_integer(monkeypatch):
    client = fake_client(entity=SimpleNamespace())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    await ops.scrape_members(
        ops.ScrapeRequest(account_id="account", target="-1001234567890")
    )
    client.get_entity.assert_awaited_once_with(-1001234567890)


@pytest.mark.asyncio
async def test_private_invite_link_is_rejected_without_calling_telegram(monkeypatch):
    client = fake_client()
    get_client = AsyncMock(return_value=client)
    monkeypatch.setattr(ops.account_manager, "get_client", get_client)
    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(
            ops.ScrapeRequest(account_id="account", target="https://t.me/+AbCdEf123456")
        )
    assert exc.value.status_code == 400
    assert "invite" in exc.value.detail.lower()
    client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolvable_target_returns_400_not_500(monkeypatch):
    client = fake_client(error=ValueError("Cannot find any entity corresponding to 'ghost'"))
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(
            ops.ScrapeRequest(account_id="account", target="@ghost")
        )
    assert exc.value.status_code == 400
    assert "could not find" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_unexpected_error_is_generic_and_does_not_leak_details(monkeypatch):
    client = fake_client(entity=SimpleNamespace())
    client.get_participants.side_effect = RuntimeError("proxy password=hunter2")
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    with pytest.raises(HTTPException) as exc:
        await ops.scrape_members(
            ops.ScrapeRequest(account_id="account", target="@example")
        )
    assert exc.value.status_code == 500
    assert "hunter2" not in exc.value.detail
    assert "RuntimeError" not in exc.value.detail
