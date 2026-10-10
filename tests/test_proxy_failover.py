import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import proxy_manager as module

OLD = {'protocol': 'socks5', 'host': '8.8.8.8', 'port': 1080, 'free': True}
NEW = {'protocol': 'socks5', 'host': '9.9.9.9', 'port': 1080}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'PROXIES_FILE', str(tmp_path / 'proxies.json'))
    clock = [1000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    p = module.ProxyManager()
    p.global_proxy = OLD.copy()
    p._save_proxies()
    p.fetch_free_candidates = AsyncMock(return_value=[NEW])
    p.test_proxy = AsyncMock(return_value={'success': False})
    return p, clock


@pytest.mark.asyncio
async def test_two_failures_then_verified_replacement_and_persisted_backoff(manager):
    p, clock = manager
    assert (await p.maintain_free_global_proxy())['status'] == 'probe_failed'
    p.fetch_free_candidates.assert_not_awaited()
    assert (await p.maintain_free_global_proxy())['status'] == 'backoff'
    clock[0] += 300
    p.test_proxy.side_effect = [{'success': False}, {'success': True}]
    assert (await p.maintain_free_global_proxy())['status'] == 'replaced'
    assert p.global_proxy['host'] == NEW['host']
    reloaded = module.ProxyManager()
    assert reloaded.global_proxy == p.global_proxy
    assert (await reloaded.maintain_free_global_proxy())['status'] == 'backoff'


@pytest.mark.asyncio
async def test_healthy_proxy_stays_sticky_and_resets_failures(manager):
    p, clock = manager
    await p.maintain_free_global_proxy()
    clock[0] += 300
    p.test_proxy.return_value = {'success': True}
    assert (await p.maintain_free_global_proxy())['status'] == 'healthy'
    assert p.global_proxy == OLD
    assert p.maintenance['consecutive_failures'] == 0
    p.fetch_free_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_working_replacement_preserves_endpoint_and_backs_off(manager):
    p, clock = manager
    await p.maintain_free_global_proxy()
    clock[0] += 300
    assert (await p.maintain_free_global_proxy())['status'] == 'no_verified_replacement'
    assert p.global_proxy == OLD
    clock[0] += 899
    assert (await p.maintain_free_global_proxy())['status'] == 'backoff'


@pytest.mark.asyncio
@pytest.mark.parametrize('manual', [True, False])
async def test_manual_and_disabled_settings_are_untouched(manager, manual):
    p, _ = manager
    if manual:
        p.set_global_proxy('http', 'example.com', 80)
    else:
        p.remove_global_proxy()
    before = p.global_proxy
    await p.maintain_free_global_proxy()
    p.test_proxy.assert_not_awaited()
    p.fetch_free_candidates.assert_not_awaited()
    assert p.global_proxy == before


@pytest.mark.asyncio
async def test_manual_change_during_discovery_wins(manager):
    p, clock = manager
    await p.maintain_free_global_proxy()
    clock[0] += 300
    async def fetch():
        p.set_global_proxy('http', 'example.com', 80)
        return [NEW]
    p.fetch_free_candidates = fetch
    p.test_proxy.side_effect = [{'success': False}, {'success': True}]
    assert (await p.maintain_free_global_proxy())['status'] == 'configuration_changed'
    assert p.global_proxy['host'] == 'example.com'
    assert module.ProxyManager().global_proxy['host'] == 'example.com'


@pytest.mark.asyncio
async def test_concurrent_checks_do_not_count_as_two_failures(manager):
    p, _ = manager
    results = await asyncio.gather(p.maintain_free_global_proxy(), p.maintain_free_global_proxy())
    assert {r['status'] for r in results} == {'probe_failed', 'backoff'}
    p.test_proxy.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_failure_preserves_proxy_and_does_not_repeat_immediately(manager):
    p, clock = manager
    await p.maintain_free_global_proxy()
    clock[0] += 300
    p.fetch_free_candidates.side_effect = RuntimeError('secret')
    r = await p.maintain_free_global_proxy()
    assert r == {'status': 'maintenance_failed', 'error': 'RuntimeError'}
    assert p.global_proxy == OLD
    assert (await p.maintain_free_global_proxy())['status'] == 'backoff'


@pytest.mark.asyncio
async def test_failed_atomic_save_retains_old_file_and_endpoint(manager, monkeypatch):
    p, clock = manager
    await p.maintain_free_global_proxy()
    clock[0] += 300
    p.test_proxy.side_effect = [{'success': False}, {'success': True}]
    monkeypatch.setattr(module.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('full')))
    assert (await p.maintain_free_global_proxy())['status'] == 'maintenance_failed'
    assert p.global_proxy == OLD
    with open(module.PROXIES_FILE) as f:
        assert json.load(f)['global'] == OLD
