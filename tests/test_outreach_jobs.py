import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from telethon import errors, types

import outreach_jobs as jobs
import outreach_policy as policy
from open_teleset.crypto.cipher import SessionCipher


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSION_ENCRYPTION_KEY', SessionCipher.generate_key())
    monkeypatch.setenv('OUTREACH_RATE_DB', str(tmp_path / 'rate.sqlite'))
    evaluator = policy.OutreachPolicy(str(tmp_path / 'policy.json'))
    doc = {'version': 1, 'approvals': {'add': {'action': 'member_add',
           'expires_at': '2099-01-01T00:00:00Z', 'account_hashes': [policy.hash_identifier('a')]}},
           'consents': [{'subject_hash': policy.hash_identifier(t), 'account_hashes': [policy.hash_identifier('a')],
           'actions': ['personal_send', 'member_add'], 'source': 'explicit',
           'granted_at': '2020-01-01T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z'} for t in ['111', '222']],
           'suppressions': []}
    evaluator.path.write_text(json.dumps(doc))
    monkeypatch.setattr(jobs, 'outreach_policy', evaluator)
    monkeypatch.setattr(policy, 'outreach_policy', evaluator)
    import telegram_ops
    monkeypatch.setattr(telegram_ops, 'outreach_policy', evaluator)
    monkeypatch.setattr(jobs, '_load_profile', AsyncMock(return_value={'role': 'admin'}))
    monkeypatch.setattr(jobs.account_manager, 'accounts', {'a': {'user_id': 999}, 'b': {'user_id': 998}})
    storage = jobs.JobStore(tmp_path / 'jobs.sqlite')
    monkeypatch.setattr(jobs, 'store', storage)
    worker = jobs.JobWorker(storage)
    client = SimpleNamespace(get_me=AsyncMock(return_value=types.User(id=999)), get_entity=AsyncMock(side_effect=lambda x: types.User(id=int(x))), send_message=AsyncMock())
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    clock = [1000.0]
    monkeypatch.setattr(jobs.time, 'time', lambda: clock[0])
    def submit(targets=None, **kwargs):
        req = jobs.BulkJobRequest(account_id='a', message='private message', targets=targets or ['111', '222'],
                                  request_id='submission_key_1234', **kwargs)
        return storage.submit('personal_send', 'owner', req, req.targets, ['a'])
    return SimpleNamespace(store=storage, worker=worker, client=client, submit=submit, clock=clock,
                           evaluator=evaluator, doc=doc, tmp_path=tmp_path)


@pytest.mark.asyncio
async def test_once_only_progress_reload_and_encrypted_payloads(env):
    job = env.submit()
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['completed'] == 1
    reloaded = jobs.JobWorker(jobs.JobStore(env.store.path))
    reloaded.store.recover()
    await reloaded.tick()
    await reloaded.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['completed'] == 2 and result['pending'] == 0
    assert env.client.send_message.await_count == 2
    assert b'private message' not in env.store.path.read_bytes()
    with env.store.db() as db:
        assert not db.execute('SELECT payload FROM jobs').fetchone()[0].startswith('{')


def test_submission_is_idempotent_and_owner_scoped(env):
    one = env.submit()
    assert env.submit() == one
    with pytest.raises(HTTPException) as error:
        env.submit(['222'])
    assert error.value.status_code == 409
    for action in ('pause', 'resume', 'stop'):
        with pytest.raises(HTTPException) as error:
            env.store.control(one, 'other', action)
        assert error.value.status_code == 404
    assert env.store.listing('other') == []


@pytest.mark.asyncio
async def test_stop_and_pause_survive_restart(env):
    job = env.submit()
    env.store.control(job, 'owner', 'pause')
    env.store.recover()
    await env.worker.tick()
    env.client.send_message.assert_not_called()
    env.store.control(job, 'owner', 'resume')
    await env.worker.tick()
    env.store.control(job, 'owner', 'stop')
    env.store.recover()
    await env.worker.tick()
    assert env.client.send_message.await_count == 1
    assert env.store.snapshot(job, 'owner')['state'] == 'stopped'
    with pytest.raises(HTTPException):
        env.store.control(job, 'owner', 'resume')


@pytest.mark.asyncio
async def test_flood_wait_pins_recipient_and_pauses_entire_job(env):
    job = env.submit()
    env.client.send_message.side_effect = [errors.FloodWaitError(None, capture=60), None, None]
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'waiting' and result['pending'] == 2 and result['retry_after'] == 60
    await env.worker.tick()
    assert env.client.send_message.await_count == 1
    env.store.control(job, 'owner', 'pause')
    env.store.control(job, 'owner', 'resume')
    await env.worker.tick()
    assert env.client.send_message.await_count == 1
    env.clock[0] += 60
    await env.worker.tick()
    assert env.client.send_message.call_args_list[0].args[0].id == env.client.send_message.call_args_list[1].args[0].id == 111
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['completed'] == 2


@pytest.mark.asyncio
async def test_restriction_never_retries_with_another_account(env):
    req = jobs.BulkJobRequest(account_ids=['a', 'b'], rotation_mode='round_robin', message='private',
        targets=['111', '222'], consent_confirmed=True, request_id='two_accounts_12345')
    job = env.store.submit('personal_send', 'owner', req, req.targets, req.account_ids)
    env.client.send_message.side_effect = errors.PeerFloodError(None)
    await env.worker.tick()
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['state'] == 'paused'
    assert jobs.account_manager.get_client.call_args_list == [(('a',),)]
    assert env.client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_unknown_delivery_is_not_replayed_or_resumable(env):
    job = env.submit()
    env.client.send_message.side_effect = TimeoutError('private data')
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'needs_review' and result['uncertain'] == 1
    assert 'private data' not in str(result)
    env.store.recover()
    await env.worker.tick()
    assert env.client.send_message.await_count == 1
    with pytest.raises(HTTPException):
        env.store.control(job, 'owner', 'resume')


@pytest.mark.asyncio
async def test_crashed_inflight_item_requires_review_before_other_recipients(env):
    job = env.submit()
    env.store.guard(job, 0, mark=True)
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['state'] == 'needs_review'
    env.client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_aliases_are_removed_from_pending(env):
    job = env.submit(['111', '111', 'alias'])
    env.client.get_entity.side_effect = lambda x: types.User(id=111)
    await env.worker.tick()
    await env.worker.tick()
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['completed'] == 1 and result['skipped'] == 2 and result['pending'] == 0
    env.client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_optout_after_submission_still_blocks_dispatch(env):
    job = env.submit(consent_confirmed=True)
    doc = env.evaluator._load()
    doc['suppressions'] = [{'subject_hash': policy.hash_identifier('111'), 'active': True}]
    env.evaluator.path.write_text(json.dumps(doc))
    await env.worker.tick()
    env.client.send_message.assert_not_called()
    assert env.store.snapshot(job, 'owner')['reason'] == 'suppressed'


@pytest.mark.asyncio
async def test_stop_during_resolution_prevents_send(env):
    job = env.submit()
    async def resolve(value):
        env.store.control(job, 'owner', 'stop')
        return types.User(id=111)
    env.client.get_entity.side_effect = resolve
    await env.worker.tick()
    env.client.send_message.assert_not_called()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'stopped' and result['pending'] == 2


@pytest.mark.asyncio
async def test_manual_stop_during_inflight_records_result_without_resuming(env):
    job = env.submit()
    async def send(*args):
        env.store.control(job, 'owner', 'stop')
    env.client.send_message.side_effect = send
    await env.worker.tick()
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'stopped' and result['completed'] == 1 and result['pending'] == 1


@pytest.mark.asyncio
async def test_revoked_operator_stops_background_job(env, monkeypatch):
    job = env.submit()
    monkeypatch.setattr(jobs, '_load_profile', AsyncMock(side_effect=PermissionError()))
    await env.worker.tick()
    jobs.account_manager.get_client.assert_not_called()
    assert env.store.snapshot(job, 'owner')['state'] == 'paused'


@pytest.mark.asyncio
async def test_offline_account_waits_without_attempting_delivery(env, monkeypatch):
    job = env.submit()
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=None))
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['retry_after'] == 30
    env.client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_existing_member_is_skipped_without_invitation(env, monkeypatch):
    req = jobs.MemberJobRequest(account_id='a', dest_target='999', usernames=['111'],
        approval_id='add', request_id='member_job_key_1234')
    job = env.store.submit('member_add', 'owner', req, req.usernames, ['a'])
    client = AsyncMock()
    client.get_me.return_value = types.User(id=999)
    client.get_entity.side_effect = lambda value: types.User(id=int(value))
    client.return_value = SimpleNamespace(participant=types.ChannelParticipant(user_id=111, date=None))
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['skipped'] == 1 and result['results'][0]['reason'] == 'already_member'
    assert client.await_count == 1
    assert isinstance(client.call_args.args[0], jobs.functions.channels.GetParticipantRequest)


@pytest.mark.asyncio
async def test_single_worker_lock_and_shutdown(env):
    other = jobs.JobWorker(env.store)
    env.worker.start()
    other.start()
    try:
        assert env.worker.task and other.task is None and other.error == 'worker_unavailable'
    finally:
        await env.worker.stop()
    other.start()
    assert other.task
    await other.stop()


@pytest.mark.asyncio
async def test_shutdown_during_send_preserves_uncertain_record(env):
    job = env.submit()
    entered = asyncio.Event()
    async def send(*args):
        entered.set()
        await asyncio.Event().wait()
    env.client.send_message.side_effect = send
    task = asyncio.create_task(env.worker.tick())
    await entered.wait()
    task.cancel()
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True
    assert cancelled
    assert env.store.snapshot(job, 'owner')['state'] == 'needs_review'


@pytest.mark.asyncio
async def test_dispatch_fence_rechecks_after_pacing_wait(env, monkeypatch):
    job = env.submit()
    # Reserve a rate slot, then Stop while the dispatcher waits for it.
    await policy.dispatch_outreach(action='personal_send', subject=111, account_id='a',
        approval_id=None, send=AsyncMock(), delay=30)
    async def wait(_):
        env.store.control(job, 'owner', 'stop')
        env.clock[0] += 30
    monkeypatch.setattr(policy.asyncio, 'sleep', wait)
    await env.worker.tick()
    env.client.send_message.assert_not_called()
    assert env.store.snapshot(job, 'owner')['state'] == 'stopped'

@pytest.mark.asyncio
async def test_revocation_during_pacing_blocks_final_dispatch(env, monkeypatch):
    job = env.submit()
    await policy.dispatch_outreach(action='personal_send', subject=111, account_id='a',
        approval_id=None, send=AsyncMock(), delay=30)
    async def wait(_):
        monkeypatch.setattr(jobs, '_load_profile', AsyncMock(side_effect=PermissionError()))
        env.clock[0] += 30
    monkeypatch.setattr(policy.asyncio, 'sleep', wait)
    await env.worker.tick()
    env.client.send_message.assert_not_called()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'paused' and result['uncertain'] == 0 and result['pending'] == 2

@pytest.mark.asyncio
async def test_shutdown_during_pacing_does_not_mark_delivery_uncertain(env, monkeypatch):
    job = env.submit()
    await policy.dispatch_outreach(action='personal_send', subject=111, account_id='a',
        approval_id=None, send=AsyncMock(), delay=30)
    async def wait(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(policy.asyncio, 'sleep', wait)
    with pytest.raises(asyncio.CancelledError):
        await env.worker.tick()
    env.client.send_message.assert_not_called()
    result = env.store.snapshot(job, 'owner')
    assert result['uncertain'] == 0 and result['pending'] == 2


@pytest.mark.asyncio
async def test_invalid_recipient_does_not_block_later_recipients(env):
    job = env.submit()
    env.client.get_entity.side_effect = [ValueError('missing'), types.User(id=222)]
    await env.worker.tick()
    await env.worker.tick()
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['failed'] == 1 and result['completed'] == 1
    env.client.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change_at_boundary', [False, True])
async def test_replaced_sender_never_dispatches(env, change_at_boundary):
    job = env.submit()
    env.client.get_me.side_effect = ([types.User(id=999), types.User(id=888)]
                                     if change_at_boundary else [types.User(id=888)])
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'paused' and result['reason'] == 'sender_identity_changed'
    assert result['pending'] == 2 and result['uncertain'] == 0
    env.client.send_message.assert_not_called()


def test_template_retry_uses_original_fingerprint_and_one_render(env, monkeypatch):
    from unittest.mock import Mock

    from template_manager import template_manager
    render = Mock(side_effect=['first time', 'later time'])
    monkeypatch.setattr(template_manager, 'render_template', render)
    first = env.submit(template_id='clock')
    assert env.submit(template_id='clock') == first
    render.assert_called_once()
    with env.store.db() as db:
        payload = env.store.unpack(db.execute('SELECT payload FROM jobs').fetchone()[0])
    assert payload['message'] == 'first time'


@pytest.mark.asyncio
async def test_completed_message_stats_survive_restart_without_double_count(env):
    import stats_tracker as stats_module
    tracker = stats_module.StatsTracker()
    tracker.stats = {}
    job = env.submit(['111'])
    await env.worker.tick()
    env.store.outcome(job, 0, 'completed')
    env.store.recover()
    await env.worker.tick()
    assert tracker.get_summary()['total_messages_sent'] == 1
    assert tracker.get_account_stats('a')['total_messages_sent'] == 1
    assert tracker.get_daily_stats()['a']['messages'] == 1
    assert tracker.get_weekly_stats()['a']['messages'] == 1
    assert tracker.get_top_accounts('messages')[0]['value'] == 1
    assert tracker.get_activity_trend('a')[-1]['messages'] == 1
    assert tracker.stats == {}


def test_unverified_sender_is_validation_error_not_idempotency_conflict(env):
    jobs.account_manager.accounts['a']['user_id'] = None
    with pytest.raises(HTTPException) as error:
        env.submit()
    assert error.value.status_code == 422
    assert env.store.listing('owner') == []


@pytest.mark.asyncio
async def test_queue_week_matches_legacy_stats_at_year_boundary(env, monkeypatch):
    from datetime import datetime

    import stats_tracker as stats_module
    class NewYear(datetime):
        @classmethod
        def now(cls):
            return cls(2027, 1, 1, 12)
    monkeypatch.setattr(jobs, 'datetime', NewYear)
    monkeypatch.setattr(stats_module, 'datetime', NewYear)
    tracker = stats_module.StatsTracker()
    tracker.stats = {}
    env.submit(['111'])
    await env.worker.tick()
    assert tracker.get_weekly_stats()['a']['messages'] == 1
