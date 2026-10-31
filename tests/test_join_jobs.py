"""Durable Auto Join jobs: submission, progress, flood waits and rotation."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from telethon import errors, functions

import outreach_jobs as jobs
import outreach_policy as policy
from open_teleset.crypto.cipher import SessionCipher


class JoinClient:
    """Callable Telethon-style client that records join requests."""

    def __init__(self, error=None):
        self.error = error
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return True

    async def get_entity(self, value):
        return SimpleNamespace(id=1, username=str(value))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSION_ENCRYPTION_KEY', SessionCipher.generate_key())
    monkeypatch.setenv('OUTREACH_RATE_DB', str(tmp_path / 'rate.sqlite'))
    evaluator = policy.OutreachPolicy(str(tmp_path / 'policy.json'))
    evaluator.path.write_text(json.dumps({'version': 1, 'approvals': {}, 'consents': [], 'suppressions': []}))
    monkeypatch.setattr(jobs, 'outreach_policy', evaluator)
    monkeypatch.setattr(policy, 'outreach_policy', evaluator)
    monkeypatch.setattr(jobs, '_load_profile', AsyncMock(return_value={'role': 'admin'}))
    monkeypatch.setattr(jobs.account_manager, 'accounts', {'a': {'user_id': 999}, 'b': {'user_id': 998}})
    storage = jobs.JobStore(tmp_path / 'jobs.sqlite')
    monkeypatch.setattr(jobs, 'store', storage)
    worker = jobs.JobWorker(storage)
    client = JoinClient()
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    clock = [1000.0]
    monkeypatch.setattr(jobs.time, 'time', lambda: clock[0])
    emitted = []
    monkeypatch.setattr(jobs.webhooks, 'emit_job', lambda operator, kind, snapshot: emitted.append((kind, snapshot)))

    def submit(urls, **kwargs):
        fields = {'request_id': 'join_submission_key_1234', 'urls': urls, **kwargs}
        if fields.get('rotation_mode') == 'round_robin':
            fields.pop('account_id', None)
        else:
            fields.setdefault('account_id', 'a')
        req = jobs.AutoJoinJobRequest(**fields)
        return storage.submit('auto_join', 'owner', req, req.urls, req.selected_accounts())

    return SimpleNamespace(store=storage, worker=worker, client=client, submit=submit, clock=clock,
                           emitted=emitted, tmp_path=tmp_path, monkeypatch=monkeypatch)


async def run_until(env, job, limit=12, step=60):
    for _ in range(limit):
        if env.store.snapshot(job, 'owner')['state'] in ('completed', 'stopped', 'needs_review'):
            return
        await env.worker.tick()
        env.clock[0] += step
    raise AssertionError('job did not reach a terminal state')


@pytest.mark.asyncio
async def test_public_and_invite_links_complete_with_timestamps(env):
    job = env.submit(['https://t.me/examplechan', 'https://t.me/+InviteHash12'])
    snapshot = env.store.snapshot(job, 'owner')
    assert snapshot['state'] == 'queued' and snapshot['total'] == 2 and snapshot['pending'] == 2
    await run_until(env, job)
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['completed'] == 2
    assert result['started'] > 0 and result['finished'] >= result['started']
    assert len(env.client.requests) == 2
    assert isinstance(env.client.requests[0], functions.channels.JoinChannelRequest)
    assert isinstance(env.client.requests[1], functions.messages.ImportChatInviteRequest)
    assert env.emitted and env.emitted[-1][0] == 'auto_join'
    assert env.emitted[-1][1]['state'] == 'completed'
    assert b'examplechan' not in env.store.path.read_bytes()


@pytest.mark.asyncio
async def test_invalid_links_are_skipped_before_dispatch(env):
    job = env.submit(['not a link!!', 'https://t.me/realchannel'])
    snapshot = env.store.snapshot(job, 'owner')
    assert snapshot['skipped'] == 1
    await run_until(env, job)
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['completed'] == 1 and result['skipped'] == 1
    assert len(env.client.requests) == 1


@pytest.mark.asyncio
async def test_already_member_is_skipped(env):
    env.client.error = errors.UserAlreadyParticipantError(None)
    job = env.submit(['https://t.me/joinedchan'])
    await run_until(env, job)
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['skipped'] == 1
    assert result['results'][0]['reason'] == 'already_member'


@pytest.mark.asyncio
async def test_flood_wait_waits_then_retries(env):
    env.client.error = errors.FloodWaitError(None, capture=60)
    job = env.submit(['https://t.me/limitedchan'])
    await env.worker.tick()
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'waiting' and result['reason'] == 'telegram_cooldown'
    assert result['retry_after'] == 60 and result['pending'] == 1
    env.client.error = None
    env.clock[0] += 60
    await run_until(env, job)
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['completed'] == 1


@pytest.mark.asyncio
async def test_rotation_uses_the_healthy_account(env):
    flooded = JoinClient(errors.FloodWaitError(None, capture=60))
    healthy = JoinClient()
    env.monkeypatch.setattr(jobs.account_manager, 'get_client',
                            AsyncMock(side_effect=[flooded, healthy, healthy]))
    job = env.submit(['https://t.me/chanone', 'https://t.me/chantwo'],
                     rotation_mode='round_robin', account_ids=['a', 'b'])
    await run_until(env, job, step=45)
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'completed' and result['completed'] == 2
    assert len(flooded.requests) <= 1 and len(healthy.requests) == 2


def test_submission_is_idempotent_and_owner_scoped(env):
    one = env.submit(['https://t.me/onechan'])
    assert env.submit(['https://t.me/onechan']) == one
    with pytest.raises(HTTPException) as error:
        env.submit(['https://t.me/twochan'])
    assert error.value.status_code == 409
    for action in ('pause', 'resume', 'stop'):
        with pytest.raises(HTTPException) as error:
            env.store.control(one, 'other', action)
        assert error.value.status_code == 404
    assert env.store.listing('other') == []


@pytest.mark.asyncio
async def test_stop_survives_restart_and_is_reported(env):
    job = env.submit(['https://t.me/onechan', 'https://t.me/twochan'])
    env.store.control(job, 'owner', 'stop')
    env.store.recover()
    await env.worker.tick()
    assert len(env.client.requests) == 0
    result = env.store.snapshot(job, 'owner')
    assert result['state'] == 'stopped' and result['finished'] > 0
    with pytest.raises(HTTPException):
        env.store.control(job, 'owner', 'resume')


def test_listing_filters_by_kind(env):
    job = env.submit(['https://t.me/onechan'])
    assert [row['id'] for row in env.store.listing('owner', 'auto_join')] == [job]
    assert env.store.listing('owner', 'member_add') == []
