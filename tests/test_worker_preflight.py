from datetime import datetime, timezone, timedelta
import pytest
from scripts.worker_preflight import verify


def probe(overrides=None):
    results = {
        "/health": (200, {"status": "ok", "service": "open-teleset", "ts": datetime.now(timezone.utc).isoformat()}),
        "/readyz": (200, {"status": "ok", "checks": {"database": True}}),
    }
    results.update(overrides or {})
    return lambda path: results.get(path, (401, None))


def test_ready_backend_authorization_contract_passes():
    verify(probe())


@pytest.mark.parametrize("readiness", [
    (503, {"status": "degraded", "checks": {"database": False}}),
    (200, {"status": "ok", "checks": {"database": False}}),
    (200, {"status": "ok", "checks": {"database": "true"}}),
])
def test_database_failure_cannot_enable_worker_only_rollout(readiness):
    with pytest.raises(RuntimeError, match="readiness"):
        verify(probe({"/readyz": readiness}))


@pytest.mark.parametrize("path", ["/api/accounts", "/api/accounts/test/export-session", "/api/schedules"])
def test_anonymous_access_cannot_enable_worker_only_rollout(path):
    with pytest.raises(RuntimeError, match="authorization"):
        verify(probe({path: (200, None)}))


def test_stale_health_cannot_enable_worker_only_rollout():
    body = {"status": "ok", "service": "open-teleset", "ts": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()}
    with pytest.raises(RuntimeError, match="stale"):
        verify(probe({"/health": (200, body)}))
