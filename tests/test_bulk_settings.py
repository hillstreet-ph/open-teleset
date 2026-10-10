from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from telethon import types

import outreach_policy as policy_module
import telegram_ops as ops
from test_outreach_execution import policy as base_policy


@pytest.fixture(name="policy")
def policy_fixture(tmp_path, monkeypatch):
    return base_policy.__wrapped__(tmp_path, monkeypatch)


def test_large_batch_accounts_and_zero_delay_are_valid():
    request = ops.BulkSendRequest(message='test', targets=[str(i) for i in range(1, 1001)],
        account_ids=[f'account-{i}' for i in range(8)], rotation_mode='round_robin', delay=0)
    assert len(request.targets) == 1000 and len(request.selected_accounts()) == 8
    assert request.per_account_limit == 0 and request.approval_id is None
    assert ops.BulkSendRequest(account_id='account', message='test', targets=['111'], delay=4000).delay == 4000


@pytest.mark.parametrize('values', [{'delay': -1}, {'delay': float('nan')}, {'delay': float('inf')},
    {'per_account_limit': -1}, {'per_account_limit': True}, {'targets': []}, {'consent_confirmed': 'true'}])
def test_invalid_settings(values):
    with pytest.raises(ValidationError):
        ops.BulkSendRequest(**{'account_id': 'account', 'message': 'test', 'targets': ['111'], **values})


@pytest.mark.asyncio
async def test_consent_receipt_allows_first_run_without_approval(policy, monkeypatch):
    policy.evaluator.path.unlink()
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=lambda value: types.User(id=int(value))),
                             send_message=AsyncMock())
    monkeypatch.setattr(ops.account_manager, 'get_client', AsyncMock(return_value=client))
    http = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id='operator')))
    result = await ops.bulk_send_personal(ops.BulkSendRequest(account_id='account', message='test',
        targets=['111', '222'], delay=0, consent_confirmed=True), http)
    assert result['sent'] == 2 and client.send_message.await_count == 2
    assert not any(policy.sleeps)
    data = policy.evaluator._load()
    assert not data['approvals']
    assert len(data['consents']) == 2
    assert {r['operator_hash'] for r in data['consents']} == {policy_module.hash_identifier('operator')}
    assert all(r['actions'] == ['personal_send'] for r in data['consents'])


@pytest.mark.asyncio
async def test_attestation_cannot_erase_optout_or_impersonate_operator(policy, monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(ops.account_manager, 'get_client', client)
    request = ops.BulkSendRequest(account_id='account', message='test', targets=['111'], consent_confirmed=True)
    with pytest.raises(HTTPException, match='Authenticated operator'):
        await ops.bulk_send_personal(request)
    policy.data['suppressions'] = [{'subject_hash': policy_module.hash_identifier('111'), 'active': True}]
    policy.save()
    http = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id='operator')))
    with pytest.raises(HTTPException, match='suppressed'):
        await ops.bulk_send_personal(request, http)
    client.assert_not_called()
    assert policy.evaluator._load()['suppressions'] == policy.data['suppressions']


def test_malformed_policy_is_never_replaced_by_attestation(policy):
    policy.evaluator.path.write_text('{broken')
    with pytest.raises(policy_module.OutreachDenied, match='policy_unavailable'):
        policy.evaluator.record_personal_consents(['111'], 'account', 'operator')
    assert policy.evaluator.path.read_text() == '{broken'


def test_large_preflight_loads_policy_once(policy, monkeypatch):
    from unittest.mock import Mock
    load = Mock(wraps=policy.evaluator._load)
    monkeypatch.setattr(policy.evaluator, '_load', load)
    decisions = policy.evaluator.authorize_many(action='personal_send', subjects=range(1000),
        account_ids=['account', 'second'], approval_id=None)
    assert len(decisions) == 2000
    load.assert_called_once()


@pytest.mark.asyncio
async def test_zero_delay_still_obeys_telegram_cooldown(policy):
    send = AsyncMock()
    policy_module.account_cooldown('account', 60)
    with pytest.raises(policy_module.OutreachDenied, match='account_cooldown'):
        await policy_module.dispatch_outreach(action='personal_send', subject='111', account_id='account',
            approval_id=None, delay=0, send=send)
    send.assert_not_called()
