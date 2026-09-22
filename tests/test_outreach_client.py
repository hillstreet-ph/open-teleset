"""Exercise the actual RPC guard without making Telegram/provider requests."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

import outreach_policy as policy_module
from outreach_client import ConsentTelegramClient, outreach_context


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    document = {
        "version": 1,
        "approvals": {
            action: {"action": action, "expires_at": "2099-01-01T00:00:00Z",
                     "account_hashes": [policy_module.hash_identifier("account")]}
            for action in policy_module.ACTIONS
        },
        "consents": [{
            "subject_hash": policy_module.hash_identifier(111), "actions": sorted(policy_module.ACTIONS),
            "source": "explicit", "granted_at": "2020-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z", "account_hashes": [policy_module.hash_identifier("account")],
        }],
        "suppressions": [],
    }
    path = tmp_path / "policy.json"

    def save():
        path.write_text(json.dumps(document))

    save()
    monkeypatch.setattr(policy_module, "outreach_policy", policy_module.OutreachPolicy(str(path)))
    monkeypatch.setenv("OUTREACH_RATE_DB", str(tmp_path / "rates.sqlite3"))
    now = [1000.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(policy_module.time, "time", lambda: now[0])
    monkeypatch.setattr(policy_module.asyncio, "sleep", sleep)
    native = AsyncMock(return_value="sent")
    monkeypatch.setattr(TelegramClient, "_call", native)
    client = ConsentTelegramClient(StringSession(), 1, "test-only-api-hash", outreach_account_id="account")
    return SimpleNamespace(client=client, native=native, data=document, save=save, sleeps=sleeps, clock=now)


def context(action="personal_send", subject=111, account_id="account"):
    return outreach_context(action=action, subject=subject, account_id=account_id, approval_id=action)


def message(peer=None, **kwargs):
    return functions.messages.SendMessageRequest(peer or types.InputPeerUser(111, 1), "test", **kwargs)


def test_constructor_forces_retries_and_flood_sleep_off():
    client = ConsentTelegramClient(
        StringSession(), 1, "test-only-api-hash", request_retries=99,
        flood_sleep_threshold=999, outreach_account_id="account",
    )
    assert client._request_retries == 0
    assert client.flood_sleep_threshold == 0
    assert client._raise_last_call_error is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rpc", [
    message(),
    functions.messages.SendMediaRequest(types.InputPeerUser(111, 1), types.InputMediaEmpty(), "test"),
    functions.messages.EditMessageRequest(types.InputPeerUser(111, 1), 1, message="test"),
    functions.messages.ForwardMessagesRequest(types.InputPeerUser(222, 1), [1], types.InputPeerUser(111, 1)),
    functions.messages.CreateChatRequest([types.InputUser(111, 1)], "test"),
    functions.channels.InviteToChannelRequest(types.InputChannel(1, 1), [types.InputUser(111, 1)]),
    functions.messages.AddChatUserRequest(1, types.InputUser(111, 1), 0),
    functions.InvokeWithoutUpdatesRequest(message()),
])
async def test_native_outreach_requires_trusted_context(guarded, rpc):
    with pytest.raises(policy_module.OutreachDenied, match="outreach_context_required"):
        await guarded.client._call(None, rpc)
    guarded.native.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_rpc_and_literal_saved_messages_are_available_without_context(guarded):
    await guarded.client._call(None, functions.updates.GetStateRequest())
    await guarded.client._call(None, message(types.InputPeerSelf()))
    assert guarded.native.await_count == 2


@pytest.mark.asyncio
async def test_saved_messages_exception_does_not_override_another_subject_context(guarded, monkeypatch):
    monkeypatch.setattr(guarded.client, "get_me", AsyncMock(return_value=SimpleNamespace(id=222)))
    with context(subject=111), pytest.raises(policy_module.OutreachDenied, match="outreach_recipient_mismatch"):
        await guarded.client._call(None, message(types.InputPeerSelf()))
    guarded.native.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_context_sends_once_and_is_cleared_afterward(guarded):
    with context():
        assert await guarded.client._call(None, message()) == "sent"
    assert guarded.native.await_count == 1
    with pytest.raises(policy_module.OutreachDenied, match="outreach_context_required"):
        await guarded.client._call(None, message())


@pytest.mark.asyncio
async def test_call_does_not_restore_automatic_retry_or_flood_sleep(guarded):
    guarded.client._request_retries = 5
    guarded.client.flood_sleep_threshold = 60
    with context():
        await guarded.client._call(None, message(), flood_sleep_threshold=600)
    assert guarded.client._request_retries == 0
    assert guarded.client.flood_sleep_threshold == 0
    assert guarded.native.await_args.kwargs["flood_sleep_threshold"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [
    {"subject": 222}, {"account_id": "other-account"}, {"action": "member_add"},
])
async def test_context_cannot_be_reused_for_wrong_peer_account_or_action(guarded, scope):
    with context(**scope), pytest.raises(policy_module.OutreachDenied, match="mismatch"):
        await guarded.client._call(None, message())
    guarded.native.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_time_optout_is_rechecked_before_send_rpc(guarded):
    async def upload_and_send():
        await guarded.client._call(None, functions.messages.UploadMediaRequest(
            types.InputPeerUser(111, 1), types.InputMediaEmpty()
        ))
        guarded.data["suppressions"] = [{"subject_hash": policy_module.hash_identifier(111), "active": True}]
        guarded.save()
        with context():
            await guarded.client._call(None, functions.messages.SendMediaRequest(
                types.InputPeerUser(111, 1), types.InputMediaEmpty(), "test"
            ))

    with pytest.raises(policy_module.OutreachDenied, match="suppressed"):
        await policy_module.dispatch_outreach(
            action="personal_send", subject=111, account_id="account",
            approval_id="personal_send", send=upload_and_send,
        )
    assert guarded.native.await_count == 1
    assert isinstance(guarded.native.await_args.args[1], functions.messages.UploadMediaRequest)


@pytest.mark.asyncio
async def test_each_album_rpc_and_list_item_is_independently_paced(guarded):
    def album():
        return functions.messages.SendMultiMediaRequest(
            types.InputPeerUser(111, 1), [types.InputSingleMedia(types.InputMediaEmpty(), "test")]
        )

    with context():
        result = await guarded.client._call(None, [album(), album()])
    assert result == ["sent", "sent"]
    assert guarded.native.await_count == 2
    assert guarded.sleeps == [3]


@pytest.mark.asyncio
async def test_album_larger_than_ten_is_denied(guarded):
    request = functions.messages.SendMultiMediaRequest(
        types.InputPeerUser(111, 1), [types.InputSingleMedia(types.InputMediaEmpty(), "test")] * 11
    )
    with context(), pytest.raises(policy_module.OutreachDenied, match="album_rpc_bounds_exceeded"):
        await guarded.client._call(None, request)
    guarded.native.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_rpc_has_one_recipient_and_uses_member_context(guarded):
    with context("member_add"):
        await guarded.client._call(None, functions.channels.InviteToChannelRequest(
            types.InputChannel(1, 1), [types.InputUser(111, 1)]
        ))
    assert guarded.native.await_count == 1
    with context("member_add"), pytest.raises(policy_module.OutreachDenied, match="one_member_per_rpc_required"):
        await guarded.client._call(None, functions.messages.CreateChatRequest(
            [types.InputUser(111, 1), types.InputUser(222, 1)], "test"
        ))
    assert guarded.native.await_count == 1


@pytest.mark.asyncio
async def test_native_future_schedule_is_denied_even_with_context(guarded):
    with context(), pytest.raises(policy_module.OutreachDenied, match="native_schedule_cannot_revalidate"):
        await guarded.client._call(None, message(schedule_date=datetime(2099, 1, 1, tzinfo=timezone.utc)))
    guarded.native.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_recipient_cannot_change_during_rate_wait(guarded, monkeypatch):
    request = message()
    with context():
        await guarded.client._call(None, request)

    async def mutate(delay):
        request.peer = types.InputPeerUser(222, 1)
        guarded.clock[0] += delay

    monkeypatch.setattr(policy_module.asyncio, "sleep", mutate)
    with context(), pytest.raises(policy_module.OutreachDenied, match="outreach_recipient_mismatch"):
        await guarded.client._call(None, request)
    assert guarded.native.await_count == 1
