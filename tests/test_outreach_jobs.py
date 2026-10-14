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
async def test_resume_explains_remaining_cooldown_without_shortening_it(env):
    job = env.submit()
    env.client.send_message.side_effect = errors.FloodWaitError(None, capture=17754)
    await env.worker.tick()
    env.store.control(job, 'owner', 'pause')
    env.clock[0] += 10
    resumed = env.store.control(job, 'owner', 'resume')
    assert resumed['state'] == 'waiting'
    assert resumed['reason'] == 'telegram_cooldown'
    assert resumed['retry_after'] == 17744
    assert resumed['retry_at'] == 18754
    assert resumed['next_account_id'] == 'a'
    await env.worker.tick()
    assert env.client.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['get_client', 'get_me', 'get_entity'])
async def test_stalled_read_yields_and_retries_same_recipient(env, monkeypatch, operation):
    job = env.submit()
    async def never_responds(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(jobs, 'TELEGRAM_READ_TIMEOUT', 0.01)
    obj = jobs.account_manager if operation == 'get_client' else env.client
    original = getattr(obj, operation)
    monkeypatch.setattr(obj, operation, AsyncMock(side_effect=never_responds))
    await asyncio.wait_for(env.worker.tick(), 1)
    result = env.store.snapshot(job, 'owner')
    assert (result['state'], result['reason'], result['pending']) == ('waiting', 'telegram_timeout', 2)
    assert result['retry_after'] == 30
    env.client.send_message.assert_not_called()
    monkeypatch.setattr(obj, operation, original)
    env.clock[0] += 30
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['completed'] == 1


@pytest.mark.asyncio
async def test_stalled_send_yields_to_other_jobs_but_never_replays(env, monkeypatch):
    job = env.submit()
    async def never_responds(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(jobs, 'TELEGRAM_SEND_TIMEOUT', 0.01)
    env.client.send_message.side_effect = never_responds
    await asyncio.wait_for(env.worker.tick(), 1)
    result = env.store.snapshot(job, 'owner')
    assert (result['state'], result['reason'], result['uncertain']) == ('needs_review', 'delivery_timeout', 1)
    env.store.recover()
    await env.worker.tick()
    assert env.client.send_message.await_count == 1
    with pytest.raises(HTTPException):
        env.store.control(job, 'owner', 'resume')
    req = jobs.BulkJobRequest(account_id='a', targets=['222'], message='other', request_id='other_submission_123')
    other = env.store.submit('personal_send', 'owner', req, req.targets, ['a'])
    env.client.send_message.side_effect = None
    await env.worker.tick()
    assert env.store.snapshot(other, 'owner')['completed'] == 1


def test_resume_preserves_scheduled_delay_and_stop_has_no_auto_resume(env):
    job = env.submit()
    env.store.waiting(job, 'account_pacing', 120)
    env.store.control(job, 'owner', 'pause')
    result = env.store.control(job, 'owner', 'resume')
    assert (result['state'], result['reason'], result['retry_after']) == ('waiting', 'scheduled_wait', 120)
    assert env.store.control(job, 'owner', 'stop')['state'] == 'stopped'


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
    # Exercise the residual race inside dispatch_outreach; the normal worker
    # path now yields before entering a known pacing wait.
    monkeypatch.setattr(jobs, 'account_pacing_remaining', lambda _: 0)
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
    monkeypatch.setattr(jobs, 'account_pacing_remaining', lambda _: 0)
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
    monkeypatch.setattr(jobs, 'account_pacing_remaining', lambda _: 0)
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
    assert result['state'] == 'completed' and result['skipped'] == 1 and result['completed'] == 1
    assert result['failed'] == 0
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


@pytest.mark.asyncio
async def test_personal_alias_binding_preserves_original_consent_window(env):
    job = env.submit(['alice_test'], consent_confirmed=True)
    source = next(row for row in env.evaluator._load()['consents']
                  if row['subject_hash'] == policy.hash_identifier('alice_test'))
    env.client.get_entity.side_effect = lambda value: types.User(id=111, access_hash=777)
    await env.worker.tick()
    data = env.evaluator._load()
    bound = next(row for row in data['consents']
                 if row['subject_hash'] == policy.hash_identifier(111)
                 and row.get('recorded_via') == 'bulk_send_form')
    assert bound['granted_at'] == source['granted_at']
    assert bound['expires_at'] == source['expires_at']
    assert env.store.snapshot(job, 'owner')['completed'] == 1


@pytest.mark.asyncio
async def test_retry_uses_persisted_input_peer_and_stable_identity(env, monkeypatch):
    job = env.submit(['alice_test'], consent_confirmed=True)
    env.client.get_entity.side_effect = lambda value: types.User(id=111, access_hash=777)
    env.client.send_message.side_effect = errors.FloodWaitError(None, capture=60)
    await env.worker.tick()
    with env.store.db() as db:
        payload = env.store.unpack(db.execute(
            'SELECT payload FROM items WHERE job=? AND seq=0', (job,)).fetchone()[0])
    assert payload['input_peer'] == {'kind': 'user', 'id': 111, 'access_hash': 777}

    fresh = SimpleNamespace(get_me=AsyncMock(return_value=types.User(id=999)),
                            get_entity=AsyncMock(return_value=types.User(id=111, access_hash=777)),
                            send_message=AsyncMock())
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=fresh))
    env.clock[0] += 60
    await env.worker.tick()
    assert isinstance(fresh.get_entity.call_args.args[0], types.InputPeerUser)
    assert env.store.snapshot(job, 'owner')['completed'] == 1


@pytest.mark.asyncio
async def test_retry_pauses_if_persisted_recipient_identity_changes(env, monkeypatch):
    job = env.submit(['alice_test'], consent_confirmed=True)
    with env.store.db() as db:
        row = db.execute('SELECT payload FROM items WHERE job=? AND seq=0', (job,)).fetchone()
        payload = env.store.unpack(row[0])
        payload.update(peer_id=111, input_peer={'kind': 'user', 'id': 111, 'access_hash': 777})
        db.execute('UPDATE items SET payload=? WHERE job=? AND seq=0', (env.store.pack(payload), job))
    fresh = SimpleNamespace(get_me=AsyncMock(return_value=types.User(id=999)),
                            get_entity=AsyncMock(return_value=types.User(id=222, access_hash=888)),
                            send_message=AsyncMock())
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=fresh))
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'paused' and result['pending'] == 1
    assert result['reason'] == 'recipient_identity_changed'
    fresh.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_paced_account_does_not_block_other_account(env, monkeypatch):
    await policy.dispatch_outreach(action='personal_send', subject=111, account_id='a',
        approval_id=None, send=AsyncMock(), delay=30)
    request_a = jobs.BulkJobRequest(account_id='a', message='one', targets=['111'],
        request_id='paced_account_job_1234')
    request_b = jobs.BulkJobRequest(account_id='b', message='two', targets=['222'],
        consent_confirmed=True, request_id='ready_account_job_1234')
    job_a = env.store.submit('personal_send', 'owner', request_a, request_a.targets, ['a'])
    job_b = env.store.submit('personal_send', 'owner', request_b, request_b.targets, ['b'])
    client_b = SimpleNamespace(get_me=AsyncMock(return_value=types.User(id=998)),
                               get_entity=AsyncMock(return_value=types.User(id=222)),
                               send_message=AsyncMock())
    clients = {'a': env.client, 'b': client_b}
    monkeypatch.setattr(jobs.account_manager, 'get_client',
                        AsyncMock(side_effect=lambda account: clients[account]))
    await env.worker.tick()
    assert env.store.snapshot(job_a, 'owner')['state'] == 'waiting'
    assert env.store.snapshot(job_a, 'owner')['reason'] == 'account_pacing'
    assert env.store.snapshot(job_b, 'owner')['completed'] == 1
    env.client.send_message.assert_not_awaited()
    client_b.send_message.assert_awaited_once()


def test_needs_review_history_is_bounded_like_other_terminal_jobs(env):
    identifiers = []
    for index in range(31):
        request = jobs.BulkJobRequest(account_id='a', message='queued', targets=['111'],
            request_id=f'review_history_{index:02d}_1234')
        identifiers.append(env.store.submit(
            'personal_send', 'owner', request, request.targets, ['a']))
    with env.store.db() as db:
        for index, identifier in enumerate(identifiers):
            db.execute("UPDATE jobs SET state='needs_review',created=? WHERE id=?", (index, identifier))
    listing = env.store.listing('owner')
    assert len(listing) == 30
    assert identifiers[0] not in {row['id'] for row in listing}


@pytest.mark.asyncio
async def test_stats_read_aggregates_without_rescanning_event_ledger(env, monkeypatch):
    import stats_tracker as stats_module
    tracker = stats_module.StatsTracker()
    tracker.stats = {}
    job = env.submit(['111'])
    await env.worker.tick()
    with env.store.db() as db:
        db.execute('DELETE FROM message_events WHERE job=?', (job,))
    reads = 0
    original = tracker._read_stats
    def count_reads():
        nonlocal reads
        reads += 1
        return original()
    monkeypatch.setattr(tracker, '_read_stats', count_reads)
    assert tracker.get_summary()['total_messages_sent'] == 1
    assert reads == 1


def test_legacy_message_events_backfill_aggregates_exactly_once(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv('SESSION_ENCRYPTION_KEY', SessionCipher.generate_key())
    path = tmp_path / 'legacy_jobs.sqlite'
    account = jobs.JobStore.pack('a')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE message_events ('
                   'job TEXT NOT NULL, seq INTEGER NOT NULL, account TEXT NOT NULL, '
                   'date TEXT NOT NULL, week TEXT NOT NULL, created TEXT NOT NULL, '
                   'PRIMARY KEY(job, seq))')
        db.execute('INSERT INTO message_events VALUES(?,?,?,?,?,?)',
                   ('legacy', 0, account, '2026-10-12', '2026-W41', '2026-10-12T00:00:00'))
    storage = jobs.JobStore(path)
    for _ in range(2):
        with storage.db() as db:
            total = db.execute('SELECT total FROM message_totals').fetchone()[0]
        assert total == 1


@pytest.mark.asyncio
async def test_rate_store_outage_keeps_recipient_pending(env, monkeypatch):
    job = env.submit(['111'])
    monkeypatch.setattr(jobs, 'account_cooldown_remaining',
                        lambda _: (_ for _ in ()).throw(policy.OutreachDenied('rate_store_unavailable')))
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'paused'
    assert result['pending'] == 1 and result['failed'] == 0
    assert result['reason'] == 'rate_store_unavailable'


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


def member_request(**kwargs):
    return jobs.MemberJobRequest(account_id='a', dest_target='999', usernames=['111'],
        request_id='automatic_member_1234', **kwargs)


def test_member_submission_selects_existing_approval_and_freezes_on_retry(env):
    request = member_request()
    job = env.store.submit('member_add', 'owner', request, request.usernames, ['a'])
    with env.store.db() as db:
        payload = env.store.unpack(db.execute('SELECT payload FROM jobs WHERE id=?', (job,)).fetchone()[0])
    assert payload['approval_id'] == 'add'
    env.doc['approvals'] = {}
    env.evaluator.path.write_text(json.dumps(env.doc))
    assert env.store.submit('member_add', 'owner', request, request.usernames, ['a']) == job
    assert request.approval_id is None


@pytest.mark.parametrize('change', ['missing', 'expired', 'wrong_account', 'wrong_action'])
def test_member_auto_selection_never_bypasses_authorization(env, change):
    approval = env.doc['approvals']['add']
    if change == 'missing':
        env.doc['approvals'] = {}
    elif change == 'expired':
        approval['expires_at'] = '2020-01-01T00:00:00Z'
    elif change == 'wrong_account':
        approval['account_hashes'] = [policy.hash_identifier('b')]
    else:
        approval['action'] = 'personal_send'
    env.evaluator.path.write_text(json.dumps(env.doc))
    request = member_request()
    with pytest.raises(policy.OutreachDenied, match='member_authorization_unavailable'):
        env.store.submit('member_add', 'owner', request, request.usernames, ['a'])
    assert env.store.listing('owner') == []


def test_member_explicit_id_still_validated(env):
    request = member_request(approval_id='1')
    with pytest.raises(HTTPException, match='approval_unknown'):
        env.store.submit('member_add', 'owner', request, request.usernames, ['a'])
    assert env.store.listing('owner') == []


@pytest.mark.asyncio
async def test_missing_member_authorization_has_actionable_http_error(env):
    env.doc['approvals'] = {}
    env.evaluator.path.write_text(json.dumps(env.doc))
    request = member_request()
    with pytest.raises(HTTPException) as error:
        await jobs.submit('member_add', 'owner', request, request.usernames, ['a'])
    assert error.value.status_code == 403
    assert 'No active member-add authorization' in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('restriction', ['consent', 'approval'])
async def test_member_selected_authorization_and_consent_rechecked_at_dispatch(env, monkeypatch, restriction):
    request = member_request()
    job = env.store.submit('member_add', 'owner', request, request.usernames, ['a'])
    if restriction == 'consent':
        env.doc['consents'] = []
    else:
        env.doc['approvals'] = {}
    env.evaluator.path.write_text(json.dumps(env.doc))
    client = AsyncMock()
    client.get_me.return_value = types.User(id=999)
    client.get_entity.side_effect = lambda value: types.User(id=int(value))
    client.side_effect = errors.UserNotParticipantError(None)
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    await env.worker.tick()
    assert client.await_count == 1
    assert isinstance(client.call_args.args[0], jobs.functions.channels.GetParticipantRequest)
    result = env.store.snapshot(job, 'owner')
    assert result['completed'] == 0 and result['state'] == 'paused'
    assert result['reason'] == ('consent_missing_or_invalid' if restriction == 'consent' else 'approval_unknown')


@pytest.mark.asyncio
async def test_member_queue_accepts_complete_list_above_ten(env, monkeypatch):
    monkeypatch.setattr(jobs, 'require_worker', lambda: None)
    targets = [str(i) for i in range(100, 350)]
    env.doc['consents'] = [dict(env.doc['consents'][0], subject_hash=policy.hash_identifier(t)) for t in targets]
    env.evaluator.path.write_text(json.dumps(env.doc))
    request = jobs.MemberJobRequest(account_id='a', dest_target='group', usernames=targets,
                                    request_id='large_member_job_1234')
    http = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id='owner')))
    result = await jobs.submit_members(request, http)
    assert result['total'] == 250 and result['pending'] == 250
    assert (await jobs.submit_members(request, http))['id'] == result['id']
    env.client.send_message.assert_not_awaited()
    env.doc['suppressions'] = [{'subject_hash': policy.hash_identifier(targets[-1]), 'active': True}]
    env.evaluator.path.write_text(json.dumps(env.doc))
    request.request_id = 'suppressed_large_job_1234'
    with pytest.raises(HTTPException) as error:
        await jobs.submit_members(request, http)
    assert error.value.status_code == 403


def test_member_job_payload_and_optional_cap_validation():
    from pydantic import ValidationError

    from telegram_ops import AddMembersRequest
    kwargs = {'account_id': 'a', 'dest_target': 'group', 'usernames': ['111'], 'request_id': 'bounded_member_1234'}
    assert jobs.MemberJobRequest(**kwargs).limit == 0
    assert jobs.MemberJobRequest(**kwargs, limit=250).limit == 250
    for value in [-1, True, 1.5]:
        with pytest.raises(ValidationError):
            jobs.MemberJobRequest(**kwargs, limit=value)
    with pytest.raises(ValidationError, match='at most 2 MB'):
        jobs.MemberJobRequest(**dict(kwargs, usernames=['1' * (2 * 1024 * 1024)]))
    with pytest.raises(ValidationError):
        AddMembersRequest(account_id='a', dest_target='group', usernames=['111'], limit=11)


@pytest.mark.asyncio
async def test_member_explicit_setup_is_scoped_idempotent_and_binds_without_renewal(env, monkeypatch):
    monkeypatch.setattr(jobs, 'require_worker', lambda: None)
    env.doc['approvals'] = {}
    env.doc['consents'] = []
    env.evaluator.path.write_text(json.dumps(env.doc))
    request = jobs.MemberJobRequest(account_id='a', dest_target='group',
        usernames=['alice_test', 'https://invalid.example', '@', '@@alice_test'], consent_confirmed=True,
        request_id='admin_member_setup_1234')
    http = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id='owner')))
    result = await jobs.submit_members(request, http)
    assert result['total'] == 4 and result['skipped'] == 3 and result['pending'] == 1
    data = env.evaluator._load()
    grant = next(iter(data['approvals']))
    before = env.evaluator.path.read_text()
    assert (await jobs.submit_members(request, http))['id'] == result['id']
    assert env.evaluator.path.read_text() == before
    assert env.evaluator.authorize(action='member_add', subject='alice_test', account_id='a', approval_id=grant).allowed
    assert not env.evaluator.authorize(action='member_add', subject='unlisted', account_id='a', approval_id=grant).allowed
    assert not env.evaluator.authorize(action='personal_send', subject='alice_test', account_id='a', approval_id=grant).allowed
    assert not env.evaluator.authorize(action='member_add', subject='alice_test', account_id='b', approval_id=grant).allowed
    for explicit in [False, True]:
        other = request.model_copy(update={'request_id': 'another_job_123456', 'dest_target': 'other_group',
            'consent_confirmed': False, 'approval_id': grant if explicit else None})
        with pytest.raises(HTTPException) as denied:
            await jobs.submit_members(other, http)
        assert denied.value.status_code == 403
    assert env.evaluator.path.read_text() == before
    env.evaluator.bind_member_peer('alice_test', 111, 'a', grant)
    after = env.evaluator._load()
    assert after['approvals'][grant]['expires_at'] == data['approvals'][grant]['expires_at']
    assert after['consents'][-1]['expires_at'] == data['consents'][0]['expires_at']
    assert env.evaluator.authorize(action='member_add', subject=111, account_id='a', approval_id=grant).allowed
    import telegram_ops
    with pytest.raises(HTTPException) as denied:
        await telegram_ops.add_members(telegram_ops.AddMembersRequest(account_id='a', dest_target='other_group',
            usernames=['111'], approval_id=grant, limit=1, delay=35))
    assert denied.value.status_code == 403
    # Legacy consent for the same peer must not replace this job's revoked consent.
    legacy_consent = dict(next(row for row in after['consents'] if row['subject_hash'] == policy.hash_identifier(111)))
    legacy_consent.pop('approval_id')
    after['consents'] = [legacy_consent]
    env.evaluator.path.write_text(json.dumps(after))
    assert not env.evaluator.authorize(action='member_add', subject=111, account_id='a', approval_id=grant).allowed
    after['approvals'].clear()
    env.evaluator.path.write_text(json.dumps(after))
    assert not env.evaluator.authorize(action='member_add', subject=111, account_id='a', approval_id=grant).allowed
    assert (await jobs.submit_members(request, http))['id'] == result['id']
    assert not env.evaluator._load()['approvals']


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing', 'deleted', 'channel', 'privacy', 'inactive', 'missing_invitee'])
async def test_member_recipient_failures_skip_and_continue(env, monkeypatch, bad):
    request = jobs.MemberJobRequest(account_id='a', dest_target='group', usernames=['111', '222'],
                                    request_id='continuous_members_1234')
    job = env.store.submit('member_add', 'owner', request, request.usernames, ['a'])
    destination = types.Channel(id=444, title='Synthetic', photo=types.ChatPhotoEmpty(), date=None)
    def resolve(value):
        if value == 'group':
            return destination
        if value == 111:
            if bad == 'missing':
                raise errors.UsernameNotOccupiedError(None)
            if bad == 'deleted':
                return types.User(id=111, deleted=True)
            if bad == 'channel':
                return destination
        return types.User(id=value)
    invites = []
    async def rpc(call):
        if isinstance(call, jobs.functions.channels.GetParticipantRequest):
            raise errors.UserNotParticipantError(None)
        invites.append(call.users[0].id)
        if call.users[0].id == 111 and bad == 'privacy':
            raise errors.UserPrivacyRestrictedError(None)
        return SimpleNamespace(missing_invitees=[111] if call.users[0].id == 111 and bad == 'missing_invitee' else [])
    client = AsyncMock(side_effect=rpc)
    client.get_me.return_value = types.User(id=999)
    client.get_entity.side_effect = resolve
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    if bad == 'inactive':
        monkeypatch.setattr(jobs, 'matches_activity', lambda entity, selected: entity.id != 111)
    await env.worker.tick()
    env.clock[0] += 36
    await env.worker.tick()
    env.clock[0] += 36
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['skipped'] == 1 and result['completed'] == 1
    assert invites[-1] == 222
    if bad in ['missing', 'deleted', 'channel', 'inactive']:
        assert invites == [222]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [errors.ChatAdminRequiredError, errors.ChannelPrivateError,
                                    errors.ChannelInvalidError, errors.ChatWriteForbiddenError])
async def test_member_destination_permission_failure_pauses_without_skipping_list(env, monkeypatch, failure):
    req = member_request()
    job = env.store.submit('member_add', 'owner', req, req.usernames, ['a'])
    client = AsyncMock(side_effect=failure(None))
    client.get_me.return_value = types.User(id=999)
    client.get_entity.side_effect = lambda value: types.User(id=111)
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'paused' and result['pending'] == 1 and result['completed'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [
    'missing', 'invalid', 'deleted', 'channel', 'privacy', 'blocked',
    'blocked_by_sender', 'write_forbidden', 'channel_private', 'deactivated',
])
async def test_personal_unavailable_recipient_skips_then_delivers_next(env, bad):
    job = env.submit(consent_confirmed=True, delay=15)
    def resolve(value):
        if value == 111:
            if bad == 'missing':
                raise errors.UsernameNotOccupiedError(None)
            if bad == 'invalid':
                raise errors.UsernameInvalidError(None)
            if bad == 'deleted':
                return types.User(id=111, deleted=True)
            if bad == 'channel':
                return types.Channel(id=111, title='Synthetic', photo=types.ChatPhotoEmpty(), date=None)
        return types.User(id=int(value))
    failures = {
        'privacy': errors.UserPrivacyRestrictedError,
        'blocked': errors.UserIsBlockedError,
        'blocked_by_sender': errors.YouBlockedUserError,
        'write_forbidden': errors.ChatWriteForbiddenError,
        'channel_private': errors.ChannelPrivateError,
        'deactivated': errors.InputUserDeactivatedError,
    }
    async def send(entity, message):
        if entity.id == 111 and bad in failures:
            raise failures[bad](None)
    env.client.get_entity.side_effect = resolve
    env.client.send_message.side_effect = send
    await env.worker.tick()
    first = env.store.snapshot(job, 'owner')
    assert first['state'] == 'running'
    assert (first['skipped'], first['pending'], first['failed']) == (1, 1, 0)
    if bad in failures:
        await env.worker.tick()
        assert env.client.send_message.await_count == 1  # retain configured pacing
    else:
        env.client.send_message.assert_not_called()
    env.clock[0] += 15
    await env.worker.tick()
    env.clock[0] += 15
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed'
    assert (result['completed'], result['skipped'], result['pending'], result['uncertain']) == (1, 1, 0, 0)
    assert env.client.send_message.call_args.args[0].id == 222
    assert env.client.send_message.await_count == (2 if bad in failures else 1)


@pytest.mark.asyncio
async def test_personal_missing_consent_still_prevents_delivery(env):
    job = env.submit()
    env.doc['consents'] = []
    env.evaluator.path.write_text(json.dumps(env.doc))
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'paused'
    assert result['reason'] == 'consent_missing_or_invalid'
    env.client.send_message.assert_not_called()


def test_member_setup_preserves_suppression_and_rejects_corrupt_policy(env):
    env.doc['suppressions'] = [{'subject_hash': policy.hash_identifier('111'), 'active': True}]
    env.evaluator.path.write_text(json.dumps(env.doc))
    before = env.evaluator.path.read_text()
    with pytest.raises(policy.OutreachDenied, match='suppressed'):
        env.evaluator.record_member_job_consents(['111'], 'a', 'owner')
    assert env.evaluator.path.read_text() == before
    env.evaluator.path.write_text('{')
    with pytest.raises(policy.OutreachDenied, match='policy_unavailable'):
        env.evaluator.record_member_job_consents(['111'], 'a', 'owner')


@pytest.mark.asyncio
async def test_member_source_revocation_after_peer_binding_prevents_invite(env, monkeypatch):
    request = jobs.MemberJobRequest(account_id='a', dest_target='group', usernames=['alice_test'],
        consent_confirmed=True, request_id='source_revoke_member_123')
    job = env.store.submit('member_add', 'owner', request, request.usernames, ['a'])
    destination = types.Channel(id=444, title='Synthetic', photo=types.ChatPhotoEmpty(), date=None)
    async def rpc(call):
        if isinstance(call, jobs.functions.channels.GetParticipantRequest):
            data = env.evaluator._load()
            data['consents'] = [row for row in data['consents'] if row['subject_hash'] != policy.hash_identifier('alice_test')]
            env.evaluator.path.write_text(json.dumps(data))
            raise errors.UserNotParticipantError(None)
        pytest.fail('Revoked source consent must prevent invitation')
    client = AsyncMock(side_effect=rpc)
    client.get_me.return_value = types.User(id=999)
    client.get_entity.side_effect = lambda value: destination if value == 'group' else types.User(id=111)
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['state'] == 'paused'
