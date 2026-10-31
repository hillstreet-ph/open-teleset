"""Webhooks: URL safety, one-time secrets, signed delivery, event mapping."""
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import webhooks
from open_teleset.crypto.cipher import SessionCipher


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('SESSION_ENCRYPTION_KEY', SessionCipher.generate_key())
    storage = webhooks.WebhookStore(tmp_path / 'webhooks.sqlite')
    monkeypatch.setattr(webhooks, 'store', storage)
    return storage


def make(store, **overrides):
    fields = {'name': 'n8n', 'url': 'https://example.com/hook',
              'events': ['join.job.completed'], **overrides}
    return store.create('owner', webhooks.WebhookCreate(**fields))


@pytest.mark.parametrize('url', [
    'http://example.com/hook',
    'https://127.0.0.1/hook',
    'https://localhost/hook',
    'https://user:pass@example.com/hook',
    'https://example.com:70000/hook',
])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(ValueError):
        webhooks._validate_url(url)


def test_secret_is_returned_once_and_stored_encrypted(store):
    created = make(store)
    assert created['secret'].startswith('owh_')
    listed = store.list('owner')
    assert listed and 'secret' not in listed[0]
    assert created['secret'].encode() not in store.path.read_bytes()
    rotated = store.rotate('owner', created['id'])
    assert rotated['secret'].startswith('owh_') and rotated['secret'] != created['secret']


def test_owner_scoping(store):
    created = make(store)
    with pytest.raises(HTTPException) as error:
        store.get('other', created['id'])
    assert error.value.status_code == 404
    with pytest.raises(HTTPException):
        store.delete('other', created['id'])
    assert len(store.list('owner')) == 1


def test_signature_is_hmac_sha256_over_timestamp_and_body(store):
    created = make(store)
    body = b'{"event":"webhook.test"}'
    expected = hmac.new(created['secret'].encode(), b'1700000000.' + body, hashlib.sha256).hexdigest()
    assert webhooks.sign(created['secret'], '1700000000', body) == expected


@pytest.mark.asyncio
async def test_delivery_is_signed_and_recorded(store, monkeypatch):
    created = make(store, events=['webhook.test'])
    captured = {}

    async def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=204)

    monkeypatch.setattr(webhooks, '_assert_public_target', lambda url: None)
    monkeypatch.setattr(webhooks, '_post', fake_post)
    hook = store.for_delivery('owner')[0]
    result = await webhooks.deliver(hook, 'webhook.test', {'message': 'hi'})
    assert result['status'] == 204
    headers = captured['headers']
    assert headers['X-OpenTeleset-Event'] == 'webhook.test'
    payload = json.loads(captured['content'])
    assert payload['data'] == {'message': 'hi'} and payload['event'] == 'webhook.test'
    signature = headers[webhooks.SIGNATURE_HEADER].removeprefix('sha256=')
    secret = store.unpack(hook['secret'])
    expected = hmac.new(secret.encode(), headers['X-OpenTeleset-Timestamp'].encode() + b'.' + captured['content'],
                        hashlib.sha256).hexdigest()
    assert signature == expected
    updated = store.list('owner')[0]
    assert updated['delivery_count'] == 1 and updated['last_status'] == 204 and updated['failure_count'] == 0


@pytest.mark.asyncio
async def test_blocked_target_is_recorded_not_retried(store, monkeypatch):
    make(store)

    def blocked(url):
        raise webhooks._BlockedTarget()

    monkeypatch.setattr(webhooks, '_assert_public_target', blocked)
    monkeypatch.setattr(webhooks, '_post', AsyncMock(side_effect=AssertionError('must not post')))
    hook = store.for_delivery('owner')[0]
    result = await webhooks.deliver(hook, 'webhook.test', {})
    assert result['status'] == 0 and result['error'] == 'blocked_target'
    updated = store.list('owner')[0]
    assert updated['failure_count'] == 1 and updated['last_error'] == 'blocked_target'


def test_emit_job_maps_terminal_states(monkeypatch):
    seen = []
    monkeypatch.setattr(webhooks, 'emit', lambda operator, event, data: seen.append((operator, event)))
    webhooks.emit_job('owner', 'auto_join', {'state': 'completed', 'failed': 0, 'uncertain': 0})
    webhooks.emit_job('owner', 'auto_join', {'state': 'completed', 'failed': 2})
    webhooks.emit_job('owner', 'auto_join', {'state': 'stopped'})
    webhooks.emit_job('owner', 'auto_join', {'state': 'running'})
    webhooks.emit_job('owner', 'member_add', {'state': 'completed'})
    assert seen == [
        ('owner', 'join.job.completed'),
        ('owner', 'join.job.failed'),
        ('owner', 'join.job.stopped'),
        ('owner', 'outreach.job.completed'),
    ]


def test_update_validates_events_and_toggles_enabled(store):
    created = make(store)
    updated = store.update('owner', created['id'], webhooks.WebhookUpdate(enabled=False))
    assert updated['enabled'] is False
    updated = store.update('owner', created['id'], webhooks.WebhookUpdate(events=['outreach.job.completed']))
    assert updated['events'] == ['outreach.job.completed']
    with pytest.raises(ValueError):
        webhooks.WebhookUpdate(events=['unknown.event'])
