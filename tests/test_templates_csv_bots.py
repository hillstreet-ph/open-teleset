from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from telethon import errors, types

import telegram_ops as ops
import template_manager as templates
from test_outreach_execution import policy as base_policy


@pytest.fixture(name="policy")
def fixture_policy(tmp_path, monkeypatch):
    return base_policy.__wrapped__(tmp_path, monkeypatch)


def test_translate_builtins_preserves_custom_edits_history_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(templates, "TEMPLATE_FILE", str(tmp_path / "templates.json"))
    manager = templates.TemplateManager()
    manager.templates = {"greeting": {"id": "greeting", "name": "问候消息", "content": "你好 {name}，现在是 {time}，祝你今天愉快！", "use_count": 8},
                         "notification": {"id": "notification", "name": "Custom", "content": "My message {date}", "use_count": 3}}
    manager.ensure_english_defaults()
    assert manager.templates["greeting"]["name"] == "Greeting"
    assert manager.templates["greeting"]["content"] == "Hello {name}, it is {time}. Have a great day!"
    assert manager.templates["greeting"]["use_count"] == 8
    assert manager.templates["notification"]["content"] == "My message {date}"
    assert manager.templates["notification"]["name"] == "Custom"
    manager.ensure_english_defaults()
    assert templates.TemplateManager().templates == manager.templates


@pytest.mark.parametrize("kwargs", [
    {"message": "", "messages": {}}, {"messages": {"333": "other"}},
    {"messages": {"111": " "}}, {"messages": {"111": "x" * 4097}},
    {"messages": {"111": "a", "@111": "b"}},
    {"template_id": "greeting", "messages": {"111": "a"}},
])
def test_invalid_csv_messages_rejected(kwargs):
    with pytest.raises(ValidationError):
        ops.BulkSendRequest(**{"account_id": "account", "message": "default", "targets": ["111"], **kwargs})


@pytest.mark.asyncio
async def test_csv_individual_message_is_delivered_only_to_approved_nonbot(policy, monkeypatch):
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=[types.User(id=111, bot=True), types.User(id=222, bot=False)]), send_message=AsyncMock())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(account_id="account", targets=["111", "222"], messages={"111": "bot message", "222": "personalized"}, approval_id="personal_send"))
    assert result["sent"] == 1 and result["skipped"] == 1
    assert result["results"][0]["skipped"] == "bot_excluded"
    assert client.send_message.call_args.args[1] == "personalized"


@pytest.mark.asyncio
async def test_adder_bot_and_activity_exclusions_never_invite(policy, monkeypatch):
    invite = AsyncMock()
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=[types.Channel(id=999, title="Synthetic", photo=types.ChatPhotoEmpty(), date=None), types.User(id=111, bot=True), types.User(id=222, bot=False)]))
    class Client:
        get_entity = client.get_entity
        __call__ = invite
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=Client()))
    result = await ops.add_members(ops.AddMembersRequest(account_id="account", dest_target="999", usernames=["111", "222"], activity_filter="7_days", approval_id="member_add"))
    assert result["skipped"] == 2 and result["added"] == 0
    assert [r["skipped"] for r in result["results"]] == ["bot_excluded", "activity_not_matched"]
    assert invite.await_count == 0


@pytest.mark.asyncio
async def test_adder_stops_after_provider_restriction(policy, monkeypatch):
    invite = AsyncMock(side_effect=errors.PeerFloodError(request=None))
    class Client:
        get_entity = AsyncMock(side_effect=[types.PeerChannel(999), types.User(id=111), types.User(id=222)])
        __call__ = invite
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=Client()))
    result = await ops.add_members(ops.AddMembersRequest(account_id="account", dest_target="999", usernames=["111", "222"], approval_id="member_add"))
    assert result["stopped_reason"] == "PeerFloodError" and result["pending"] == 1
    assert invite.await_count == 1


@pytest.mark.asyncio
async def test_numeric_csv_ids_are_not_interpreted_as_phone_contacts(policy, monkeypatch):
    client = SimpleNamespace(get_entity=AsyncMock(return_value=types.User(id=111)), send_message=AsyncMock())
    monkeypatch.setattr(ops.account_manager, "get_client", AsyncMock(return_value=client))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(account_id="account", targets=["111"], messages={"111": "CSV"}, approval_id="personal_send"))
    client.get_entity.assert_awaited_once_with(111)
    assert result["sent"] == 1


@pytest.mark.asyncio
async def test_template_only_request_renders_and_invalid_empty_template_never_sends(policy, monkeypatch):
    from fastapi import HTTPException
    client = SimpleNamespace(get_entity=AsyncMock(return_value=types.User(id=111)), send_message=AsyncMock())
    monkeypatch.setattr(ops.account_manager, 'get_client', AsyncMock(return_value=client))
    monkeypatch.setattr(templates.template_manager, 'render_template', lambda *args, **kwargs: 'Rendered')
    request = ops.BulkSendRequest(account_id='account', targets=['111'], template_id='greeting', approval_id='personal_send')
    assert (await ops.bulk_send_personal(request))['sent'] == 1
    assert client.send_message.call_args.args[1] == 'Rendered'
    client.send_message.reset_mock()
    monkeypatch.setattr(templates.template_manager, 'render_template', lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as error:
        await ops.bulk_send_personal(request)
    assert error.value.status_code == 400
    assert client.send_message.await_count == 0
