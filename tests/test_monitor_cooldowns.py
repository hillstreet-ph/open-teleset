from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors
import outreach_policy as policy
import health_monitor as module


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTREACH_RATE_DB', str(tmp_path / 'rate.sqlite'))
    monkeypatch.setattr(module, 'HEALTH_FILE', str(tmp_path / 'health.json'))
    m = module.HealthMonitor()
    m._save_health = Mock()
    return m


@pytest.mark.asyncio
async def test_monitor_skips_provider_cooldown_without_connect_or_proxy_change(monitor, monkeypatch):
    policy.account_cooldown('a', 60)
    get_client = AsyncMock()
    monkeypatch.setattr(module.account_manager, 'get_client', get_client)
    result = await monitor.check_account_health('a')
    assert result['status'] == 'cooldown' and result['retry_after'] > 0
    get_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_read_probe_persists_flood_wait(monitor, monkeypatch):
    client = SimpleNamespace(get_me=AsyncMock(side_effect=errors.FloodWaitError(None, capture=90)))
    monkeypatch.setattr(module.account_manager, 'get_client', AsyncMock(return_value=client))
    assert (await monitor.check_account_health('a'))['status'] == 'cooldown'
    assert 89 < policy.account_cooldown_remaining('a') <= 90
    assert not monitor.health_data.get('a', {}).get('banned', False)


def test_cooldown_store_corruption_fails_closed(tmp_path, monkeypatch):
    p = tmp_path / 'rate.sqlite'
    p.write_text('not sqlite')
    monkeypatch.setenv('OUTREACH_RATE_DB', str(p))
    with pytest.raises(policy.OutreachDenied, match='rate_store_unavailable'):
        policy.account_cooldown_remaining('a')


@pytest.mark.asyncio
async def test_monitor_runs_maintenance_and_cancels_background_task(monitor, monkeypatch):
    import asyncio
    called = asyncio.Event()
    async def maintain():
        called.set()
        return {'status': 'healthy'}
    monkeypatch.setattr(module.proxy_manager, 'maintain_free_global_proxy', maintain)
    monkeypatch.setattr(module.account_manager, 'accounts', {})
    await monitor.start_monitoring(interval=300)
    task = monitor._task
    await asyncio.wait_for(called.wait(), 1)
    monitor.stop_monitoring()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert monitor._task is None


@pytest.mark.asyncio
async def test_monitor_checks_assigned_proxy_before_global(monitor, monkeypatch):
    client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='fixture', phone=None)))
    monkeypatch.setattr(module.account_manager, 'get_client', AsyncMock(return_value=client))
    assigned = {'protocol':'socks5', 'host':'9.9.9.9', 'port':1080, 'assigned_to':['a']}
    monkeypatch.setattr(module.proxy_manager, 'proxies', {'dedicated':assigned})
    monkeypatch.setattr(module.proxy_manager, 'global_proxy', {'protocol':'socks5', 'host':'8.8.8.8', 'port':1080})
    probe = AsyncMock(return_value={'success':True})
    monkeypatch.setattr(module.proxy_manager, 'test_proxy', probe)
    assert (await monitor.check_account_health('a'))['status'] == 'healthy'
    probe.assert_awaited_once_with(assigned)
